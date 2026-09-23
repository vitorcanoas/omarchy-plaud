#!/usr/bin/env python3
"""
plaud-linux :: engine de captura de áudio (nativo PipeWire/PulseAudio via ffmpeg)

Replica o comportamento do Plaud Desktop no Linux:
  - captura o ÁUDIO DO SISTEMA (o que toca nos alto-falantes) pelo .monitor do sink
  - opcionalmente mixa o MICROFONE (reuniões onde você fala)
  - grava direto em Opus (arquivo pequeno; ótimo p/ transcrição)
  - suporta pausar/retomar (concatena segmentos no stop)

Sem dependência de rede. Só ffmpeg + pactl (pulseaudio-utils, que fala com o pipewire-pulse).
"""
import json
import os
import signal
import subprocess
import threading
import time
from datetime import datetime

try:
    from . import paths
except ImportError:  # allow running as a plain script
    import paths

REC_DIR = paths.RECORDINGS
LOG_DIR = paths.LOGS


class MissingSystemMonitor(RuntimeError):
    """The requested system output has no capture monitor yet."""


def write_json(path, obj):
    """Write `obj` as JSON so a reader never sees a half-written file.

    write_text() truncates the target and only then writes, so every reader
    during that window -- and every crash inside it -- sees a partial file.
    Measured against the session sidecar with two differently-sized writers
    racing one reader: 200/200 trials produced unparseable JSON. Writing a
    sibling temp file and os.replace()-ing it onto the target is atomic on
    POSIX within one filesystem, so a reader gets the whole old file or the
    whole new one. The temp file is a sibling, not /tmp, because os.replace()
    is only atomic within a filesystem.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, indent=2, ensure_ascii=False))
            f.flush()
            # Ordering only. Without it the rename can land before the bytes,
            # so a crash leaves an intact-looking sidecar full of zeros.
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Never leave the temp behind: recordings/ is the user's directory and
        # a stale .json.tmp there looks like a recording that half-happened.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def read_json(path):
    """Read a sidecar, preserving it untouched if it will not parse.

    NEVER rebuild a sidecar you cannot read. The notes the user typed live
    only here, and reconstructing the file from what the code can infer
    silently destroys them -- strictly worse than failing loudly. An
    unparseable file is renamed aside so a human still has it, and None is
    returned so the caller has to decide rather than being handed a guess.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, OSError, UnicodeDecodeError):
        keep = path.with_name(f"{path.name}.corrupt.{int(time.time())}")
        try:
            os.replace(path, keep)
        except OSError:
            pass
        return None


def _run(cmd):
    # pactl translates its own labels: under pt_BR "Sink Input #" prints as
    # "Entrada do destino #", "Sink:" as "Destino:", "Corked:" as "Cork:" and
    # "Mute:" as "Mudo:". playing_sink() has to read that verbose listing --
    # it is the only place cork and mute are reported -- and it matches the
    # English labels, so without this pin it finds nothing and the app falls
    # back to recording the wrong device.
    env = dict(os.environ, LC_ALL="C", LANGUAGE="C")
    try:
        return subprocess.run(cmd, capture_output=True, text=True, env=env).stdout
    except FileNotFoundError:
        # pactl ships in pulseaudio-utils, a different package from pipewire, so
        # it can genuinely be absent. Every caller already treats empty output as
        # "nothing found" and falls through; letting the exception escape instead
        # aborted Overlay.__init__ -- which calls start() at overlay.py:128 before
        # show_all() -- so the pill never appeared and the only trace was a
        # traceback in app.log. Empty output degrades to `-i default` instead.
        return ""


def default_sink():
    """Nome do sink padrão do sistema."""
    out = _run(["pactl", "get-default-sink"]).strip()
    return out or None


def list_sinks():
    """[(name, description)] de todos os sinks (saídas)."""
    sinks = []
    for line in _run(["pactl", "list", "sinks", "short"]).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            sinks.append(parts[1])
    return sinks


def running_sinks():
    """Sinks em estado RUNNING — placa ativa, com áudio passando por ela."""
    out = []
    for line in _run(["pactl", "list", "sinks", "short"]).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[-1].strip().upper() == "RUNNING":
            out.append(parts[1])
    return out


def list_sources():
    """Nomes de todas as sources (inclui .monitor e microfones)."""
    srcs = []
    for line in _run(["pactl", "list", "sources", "short"]).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            srcs.append(parts[1])
    return srcs


def default_source():
    """Microfone padrão (source que não é monitor)."""
    out = _run(["pactl", "get-default-source"]).strip()
    return out or None


def playing_sink():
    """
    Descobre em QUAL sink há áudio tocando agora (sink-inputs ativos).
    Retorna o node.name do sink que está reproduzindo, ou None.
    É a heurística que o Plaud usa: seguir o áudio que está de fato tocando.
    """
    # `pactl list short` is tab-separated machine output and is never
    # translated, unlike the verbose listing whose every label is localized.
    id2name = {}
    for line in _run(["pactl", "list", "sinks", "short"]).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            id2name[parts[0].strip()] = parts[1]
    # The short sink-input listing has no Corked/Mute column, and a paused or
    # muted stream keeps its sink-input alive: taking it records -91 dB. Only
    # the verbose listing reports both, so read that -- `_run` pins LC_ALL=C,
    # which is what makes its labels English here whatever the desktop speaks.
    # Take nothing but the ids from it: pactl prints a stream's own metadata
    # raw, newlines included, so a track title can forge an entire block.
    silent = set()
    seen = set()
    cur = None
    for line in _run(["pactl", "list", "sink-inputs"]).splitlines():
        if line.startswith("Sink Input #"):
            sid = line.split("#", 1)[1].strip()
            # A real id heads exactly one block, so a repeat is a forged header
            # aimed at another stream: attribute the lines after it to nobody.
            cur = None if sid in seen else sid
            seen.add(sid)
        elif line.startswith(("\tCorked: yes", "\tMute: yes")):
            silent.add(cur)
    # The sink id comes from the short listing instead -- one tab-separated
    # line per stream, where no title can inject anything.
    paused = None
    for line in _run(["pactl", "list", "sink-inputs", "short"]).splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or parts[1].strip() not in id2name:
            continue
        if parts[0].strip() not in silent:
            return id2name[parts[1].strip()]
        # A paused stream still says which card the audio will come out of.
        # Weaker evidence, so keep it only if nothing is actually playing --
        # dropping it altogether sent the blind fallback to a suspended card.
        if paused is None:
            paused = id2name[parts[1].strip()]
    return paused


def monitor_of(sink_name):
    """Nome da source .monitor de um sink."""
    if not sink_name:
        return None
    return f"{sink_name}.monitor"


def pick_system_monitor():
    """
    Escolhe o melhor monitor p/ capturar o áudio do sistema:
      1) se há algo tocando agora, usa o sink que está tocando (mais preciso)
      2) senão, usa o sink default
      3) evita cair no monitor do próprio microfone (Fifine tem saída, mas não toca aula)

    O filtro `fifine` NÃO se aplica aos passos 1 e 2, e a razão é a mesma nos
    dois: o Fifine K690 é placa de som de verdade -- tem saída de fone -- e não
    só microfone. Um sink que está tocando, ou que o usuário escolheu como
    padrão, é a saída real do sistema, e o nome dele não muda isso.

    Filtrar no passo 1 gravava o HDMI suspenso: silêncio digital (-91 dB),
    exatamente o que o filtro existia para evitar. Filtrar no passo 2 fazia o
    mesmo, e foi medido em 2026-09-06 com o usuário assistindo: sem nada
    tocando, o Fifine como padrão foi rejeitado pelo nome, a escolha caiu no
    HDMI suspenso, e a sessão gravou um seg_000.opus de ZERO bytes. Print e
    bandeira funcionaram na mesma sessão; só o áudio se perdeu.

    O filtro sobrou onde ainda faz sentido: escolher às cegas entre sinks que
    ninguém pediu e nada está tocando (passo 4). Ali, e só ali, "não é o
    microfone" é um desempate melhor que a ordem alfabética.
    """
    ps = playing_sink()
    if ps:
        return monitor_of(ps), ps, "tocando-agora"
    # Nada tocando agora: o sink PADRÃO é a resposta, sem filtro de nome. É a
    # saída que o sistema usaria se algo começasse a tocar neste instante, que
    # é precisamente o que uma gravação de "áudio do sistema" quer capturar.
    ds = default_sink()
    if ds:
        return monitor_of(ds), ds, "sink-default"
    # Sem padrão definido: prefira um sink RUNNING (placa ativa) a um SUSPENDED.
    # Gravar um sink suspenso rende silêncio, que é o pior resultado possível.
    running = running_sinks()
    if running:
        running_no_fifine = [s for s in running if "fifine" not in s.lower()]
        if running_no_fifine:
            return monitor_of(running_no_fifine[0]), running_no_fifine[0], "sink-running"
        return monitor_of(running[0]), running[0], "sink-running"
    # fallback: primeiro sink que não seja o microfone
    for s in list_sinks():
        if "fifine" not in s.lower():
            return monitor_of(s), s, "primeiro-nao-mic"
    # último recurso: qualquer sink, mesmo o do microfone. Um monitor de mic
    # ainda pode ter áudio; nenhum sink não tem nada.
    todos = list_sinks()
    if todos:
        return monitor_of(todos[0]), todos[0], "ultimo-recurso"
    return None, None, "sem-sink"


# The official client's own constant (recordingService-Mile2cgU.js:183): the
# flag transcribes the last MARK_SECONDS of audio. Not configurable there, so
# not configurable here.
MARK_SECONDS = 40
MARK_RATE = 48000
MARK_BYTES = MARK_RATE * 2 * MARK_SECONDS  # s16le mono


class MarkBuffer:
    """A rolling in-memory ring of the last MARK_SECONDS of raw PCM.

    Fed by a SECOND ffmpeg reading the same source the session records. The
    obvious alternative -- tailing the segment ffmpeg is writing -- is closed:
    ffmpeg emits the Ogg container only at a clean exit, so the live segment is
    0 bytes for its whole duration (see _concat). A capture started when the
    button is pressed is equally wrong: the flag is retroactive, it must return
    the audio that came BEFORE the press.

    Everything here is best-effort. The recording outranks this feature
    absolutely: a tap that fails to spawn, dies mid-session or is killed must
    leave the capture untouched and the user's .opus intact. Hence the bare
    `except Exception` around the spawn and the pump loop -- there is no tap
    failure worth propagating into the recording path.
    """

    def __init__(self, input_args, log_path=None):
        self._input_args = input_args
        self._log_path = log_path
        self.proc = None
        self._buf = bytearray()
        self._lock = threading.Lock()

    def _log(self, msg):
        if not self._log_path:
            return
        try:
            with open(self._log_path, "a") as lf:
                lf.write(f"\n[{datetime.now()}] MARK {msg}\n")
        except OSError:
            pass

    def start(self):
        """Spawn the tap. Never raises -- a failure here must not stop start()."""
        if self.proc is not None:
            return
        cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error"] + list(self._input_args)
               + ["-ac", "1", "-ar", str(MARK_RATE), "-f", "s16le", "-"])
        try:
            # stderr to DEVNULL, not a pipe: nobody drains a pipe here, so a
            # chatty ffmpeg would fill the 64K pipe buffer and block the tap
            # forever -- and a wedged tap holding a PulseAudio stream is
            # exactly the kind of thing that could disturb the capture.
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL,
                                         stdin=subprocess.DEVNULL)
        except Exception as e:
            self.proc = None
            self._log(f"tap spawn failed: {e!r}")
            return
        # Daemon, and deliberately not held: it ends when the pipe closes,
        # and nothing may join it -- a stop() that waited on this thread could
        # stall the recording's teardown.
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        """Read the tap into the ring. Daemon thread; exits when the tap does."""
        try:
            while True:
                block = proc.stdout.read(8192)
                if not block:
                    break
                with self._lock:
                    self._buf.extend(block)
                    if len(self._buf) > MARK_BYTES:
                        del self._buf[:len(self._buf) - MARK_BYTES]
        except Exception as e:
            self._log(f"tap pump ended: {e!r}")
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

    def snapshot(self):
        """The buffered PCM, newest MARK_SECONDS. Measured at 0.9 ms -- safe on
        the GTK thread, which is where the flag button will call it."""
        with self._lock:
            return bytes(self._buf)

    def stop(self):
        """Kill the tap and let the pump drain out. Never raises.

        SIGKILL, unlike the capture's SIGINT: there is no container to finalize
        -- the tap writes raw PCM to a pipe -- so nothing is lost by killing it,
        and it must never be able to delay stop() of the real recording.
        """
        proc = self.proc
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
            # Do not forget a tap that may still be reading microphone/system
            # audio. Unlike the main Opus capture this is raw PCM, so killing
            # it needs no container finalization, but exit still needs proof.
            try:
                if proc.poll() is None:
                    return False
            except Exception:
                return False
            self.proc = None
        return True

    def dump_ogg(self, path):
        """Transcode the ring to Ogg/Opus at the path. None if there is nothing.

        Encoding matches what the backend expects and what the official client
        sends for a mark (PLAUD_API_NOTES §AudioMark): s16le 48k mono in,
        libopus 16 kHz mono VoIP out -- the same settings the main capture uses.
        An empty buffer returns None so the caller skips it with no network
        call, mirroring the official client's own zero-length bail.
        """
        pcm = self.snapshot()
        if not pcm:
            return None
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "s16le", "-ar", str(MARK_RATE), "-ac", "1", "-i", "-",
               "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k",
               "-application", "voip", str(path)]
        try:
            r = subprocess.run(cmd, input=pcm, capture_output=True)
        except Exception as e:
            self._log(f"dump_ogg spawn failed: {e!r}")
            return None
        # ffmpeg can exit 0 having written nothing (the project's own recorded
        # lesson: exit code 0 is not success). Check the artifact.
        if r.returncode != 0 or not path.exists() or path.stat().st_size == 0:
            self._log(f"dump_ogg failed rc={r.returncode}")
            return None
        return path


class Recorder:
    """
    Gravador com pausa/retomada. Cada segmento é um processo ffmpeg;
    no stop, os segmentos são concatenados num único .opus final.
    """

    def __init__(self, mode="system", mic=False, name=None):
        # mode: "system" (áudio do PC) | "mic" (só microfone) | "meeting" (sistema+mic)
        self.mode = mode
        self.mic = mic or mode == "meeting"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session = name or f"gravacao_{ts}"
        # Two sessions started in the same second would otherwise share segdir
        # and final_path, and the second would overwrite the first mid-recording.
        if name is None:
            n = 1
            while (REC_DIR / f"{self.session}.opus").exists() or (REC_DIR / f".seg_{self.session}").exists():
                n += 1
                self.session = f"gravacao_{ts}_{n}"
        self.ts = ts
        self.segdir = REC_DIR / f".seg_{self.session}"
        self.segdir.mkdir(parents=True, exist_ok=True)
        self.segments = []
        self._src = None
        self.proc = None
        self.started_at = None
        self.paused_accum = 0.0
        self.paused_at = None
        self.state = "idle"  # idle|recording|paused|stopped
        self.final_path = REC_DIR / f"{self.session}.opus"
        self.meta_path = REC_DIR / f"{self.session}.json"
        self.screenshots = []
        self.notes = []
        self.flags = []
        self.mark_buffer = None
        self.upload = None
        self.concat_failed = False
        self.segments_lost = 0
        self._merged = False
        self._save_meta()

    def _resolve_src(self):
        """Resolve the capture source once per session, then reuse it.

        Re-picking on every resume let a session change what it was recording
        midway: pause, the video stops, resume -> playing_sink() is now None and
        the fallback chain can land on a different card. The audio survives
        (concat tolerates it) but half the recording is of the wrong source.

        The cost, stated so nobody has to rediscover it: re-probing was also the
        only thing that ever corrected a bad first pick, and pinning the source
        gives that up. Everything the pick depends on must therefore be right at
        `start()`, which is why a failed probe below is not cached.
        """
        if self._src is None or (self.mode != "mic" and not self._src[0]):
            mon, sink, why = pick_system_monitor()
            mic_src = getattr(self, "mic_device", None) or default_source()
            # An empty probe is a failure, not an answer. Freezing it would pin
            # the session to `-i default`, which pulse resolves to the default
            # *source* -- the microphone -- so a system-audio session would
            # record the room for its whole length even after pactl recovers.
            if not (mic_src if self.mode == "mic" else mon):
                return (mon, sink, mic_src, why)
            self._src = (mon, sink, mic_src, why)
        return self._src

    def _input_args(self):
        """The input half of the capture command: `-i` plus any mix filter.

        Factored out so the mark tap reads the SAME stream this session is
        recording instead of re-picking a source of its own. A meeting mixes
        system + mic here; a tap that re-picked would take the monitor alone,
        so a flag would silently omit the user's own voice -- audio that looks
        fine and is wrong.
        """
        mon, sink, mic_src, why = self._resolve_src()
        if self.mode != "mic" and not mon:
            # `-i default` is Pulse's default *source*, often the microphone.
            # Never substitute it for a requested system-output monitor.
            raise MissingSystemMonitor("system output monitor unavailable")
        if self.mode == "mic":
            args = ["-f", "pulse", "-i", mic_src or "default"]
        elif self.mic and mon and mic_src:
            # sistema + microfone mixados
            args = ["-f", "pulse", "-i", mon, "-f", "pulse", "-i", mic_src,
                    "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[a]",
                    "-map", "[a]"]
        else:
            args = ["-f", "pulse", "-i", mon or "default"]
        return args, mon, sink, mic_src, why

    def _ffmpeg_cmd(self, out):
        args, mon, sink, mic_src, why = self._input_args()
        common = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        enc = ["-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", "-application", "voip", str(out)]
        self._last_src = (mon, sink, mic_src, why)
        return common + args + enc

    def start(self):
        if self.state == "recording":
            return
        seg = self.segdir / f"seg_{len(self.segments):03d}.opus"
        cmd = self._ffmpeg_cmd(seg)
        logf = open(LOG_DIR / f"{self.session}.log", "a")
        logf.write(f"\n[{datetime.now()}] START seg={seg.name} cmd={' '.join(cmd)}\n")
        logf.flush()
        self.proc = subprocess.Popen(cmd, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL)
        self.segments.append(seg)
        # After the capture is up, never before: the tap is optional and the
        # recording is not, so nothing about it may run ahead of the process
        # that produces the audio.
        self._start_mark_buffer()
        if self.started_at is None:
            self.started_at = time.time()
        elif self.paused_at is not None:
            self.paused_accum += time.time() - self.paused_at
            self.paused_at = None
        self.state = "recording"
        self._save_meta()

    def pause(self):
        if self.state != "recording" or not self.proc:
            return
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
        # Requirement, not tidiness: the tap keeps filling while the capture is
        # stopped, so a flag pressed just after resume would return audio that
        # is not in the file, timestamped against an elapsed() that froze. The
        # official client greys the flag out while paused for the same reason.
        self._stop_mark_buffer()
        self.paused_at = time.time()
        self.state = "paused"
        self._save_meta()

    def resume(self):
        if self.state == "paused":
            self.start()

    def set_sources(self, system, mic):
        """Change what is being captured, mid-session. Returns True if applied.

        The official client's two toggles are live while it records, so this
        has to work on a running Recorder and not only before start().

        Implemented on top of the segment machinery that pause/resume already
        uses, because that is the only cut point where the capture command can
        change: a running ffmpeg cannot be re-pointed at a different input.
        Segment N finishes on the old sources, segment N+1 opens on the new
        ones, and stop() concatenates them as it always has. `_input_args()`
        re-reads self.mode/self.mic on every spawn, so nothing else has to know.

        Refuses (False) when both are off, and this is a DELIBERATE divergence
        from the official client rather than a missing feature.

        The official client allows both off. It always opens the native
        recorder with `enableMicrophone: true, enableSystemAudio: true` as
        literal constants and then mutes each channel live
        (recordingService-Mile2cgU.js start(); the settings are
        enableSystemAudioRecordAtom, default true, and micDeviceStateAtom,
        default "smart"/Automatic with "Off" as one device option). With both
        muted it enters a "full silence" state -- detectFullSilence() snapshots
        the elapsed time, and on re-enable fillSilenceGapIfNeeded() pads the
        hole with recorder.fillSilence(gapMs) so the file stays as long as the
        clock says it is.

        That depends on a recorder that can be told to emit silence. Ours is
        ffmpeg segments concatenated with -c copy, which cannot manufacture a
        gap. Reproducing "both off" here would therefore desynchronise the
        audio from elapsed() -- the clock every screenshot and note is stamped
        against -- so the honest local answer is to keep at least one channel
        live. See Overlay._sync_src_ui, which keeps the state unreachable in
        the UI; this is the backstop, not the primary guard.

        A no-op change returns True without touching the capture: re-spawning
        ffmpeg for the same sources would split the file for nothing, and every
        split is a concat seam that can fail.

        The cost of a split, measured rather than estimated: a 9 s session cut
        twice (system -> meeting -> mic) produced a 7.19 s file, so each cut
        loses roughly 0.6 s of audio to ffmpeg's teardown and respawn. That is
        the same loss pause/resume has always had, and it is why the no-op
        early return above matters -- but it also means elapsed() runs slightly
        ahead of the file after a switch, and screenshot/note timestamps drift
        by that much. Acceptable for a handful of deliberate toggles; it would
        not be if anything ever called this on a timer.

        Deliberately does NOT clear self._src. The device probe is pinned for
        the session on purpose (see _resolve_src), and this changes which of the
        already-probed devices are used, not which devices exist.
        """
        if not (system or mic):
            return False
        mode = "meeting" if (system and mic) else ("system" if system else "mic")
        want_mic = bool(mic)
        if mode == self.mode and want_mic == self.mic:
            return True
        if mode != "mic" and not (self._src and self._src[0]):
            # Validate before pausing the current microphone segment. A
            # rejected toggle must leave that capture running, and a later
            # explicit attempt may re-probe after the sink returns.
            mon, sink, why = pick_system_monitor()
            if not mon:
                return False
            mic_src = getattr(self, "mic_device", None) or default_source()
            self._src = (mon, sink, mic_src, why)
        # Paused or idle: record the choice and let the next start() apply it.
        # Cutting a segment here would append an empty one to a paused session.
        if self.state != "recording":
            self.mode = mode
            self.mic = want_mic
            self._save_meta()
            return True
        # SIGINT, not kill: Opus needs the clean finalize, exactly as pause()
        # does. Reusing pause()/start() rather than open-coding the cut keeps
        # the mark-buffer teardown and the segment bookkeeping in one place.
        self.pause()
        self.mode = mode
        self.mic = want_mic
        # pause() charged the gap to paused_accum, so elapsed() would jump
        # backwards by the split's own duration -- a timer that ticks down
        # mid-recording. The user never asked for a pause and none happened, so
        # the accounting must not show one.
        if self.paused_at is not None:
            self.paused_at = time.time()
        self.start()
        return True

    def _start_mark_buffer(self):
        """Bring the mark tap up. Swallows everything: the recording outranks it.

        A fresh MarkBuffer per segment, so resume() starts with an empty ring --
        the audio captured during the pause is exactly what must not be in it.
        """
        tap = self.mark_buffer
        if tap is not None:
            proc = getattr(tap, "proc", None)
            if proc is not None:
                try:
                    if proc.poll() is None:
                        return
                except Exception:
                    return
            # The old tap is confirmed out (or never spawned). Only then may
            # resume replace its handle with a new segment's ring.
            self.mark_buffer = None
        try:
            args, _mon, _sink, _mic, _why = self._input_args()
            self.mark_buffer = MarkBuffer(args, LOG_DIR / f"{self.session}.log")
            self.mark_buffer.start()
        except Exception:
            # Nothing a failed tap can do is worth failing start() over: the
            # user would lose the recording to gain nothing (Principle II).
            self.mark_buffer = None

    def _stop_mark_buffer(self):
        tap = self.mark_buffer
        if tap is None:
            return True
        try:
            tap.stop()
        except Exception:
            pass
        # A failed kill/wait must not lose the only handle to a tap that can
        # still capture. A later explicit Stop retries it.
        proc = getattr(tap, "proc", None)
        if proc is not None:
            try:
                if proc.poll() is None:
                    return False
            except Exception:
                return False
        self.mark_buffer = None
        return True

    def elapsed(self):
        """Segundos de áudio gravado — congela na pausa, para bater com o arquivo."""
        if self.started_at is None:
            return 0
        ref = self.paused_at if self.paused_at is not None else time.time()
        return int(ref - self.started_at - self.paused_accum)

    def add_screenshot(self, path):
        self.screenshots.append({"path": str(path), "t": self.elapsed(), "at": datetime.now().isoformat()})
        self._save_meta()

    def add_note(self, text, t):
        # `t` is passed in: the note is timestamped when the user opened the box,
        # not when they finished typing, which can be a minute later.
        self.notes.append({"text": text, "t": int(t), "at": datetime.now().isoformat()})
        self._save_meta()

    def add_flag(self, path, t):
        """Record a flag: the .ogg snippet dumped from the ring, and when.

        Shaped like add_screenshot/add_note because phase 3 uploads all three
        from the same batch. `t` is passed in for the same reason it is on
        add_note: the snippet is timestamped at the press, and whatever reads
        it later must not re-derive an elapsed() that has since moved on.
        """
        self.flags.append({"path": str(path), "t": int(t), "at": datetime.now().isoformat()})
        self._save_meta()

    def stop(self):
        if self.proc:
            proc = self.proc
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                # The child may have exited between the stop click and SIGINT.
                # If its state is uncertain, retain the handle for a retry.
                try:
                    if proc.poll() is None:
                        raise
                except OSError:
                    raise
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                # SIGKILL discards an unfinished Opus container. Keep the
                # indicator and allow another graceful attempt instead.
                try:
                    if proc.poll() is None:
                        raise
                except OSError:
                    raise
            self.proc = None
        if self._stop_mark_buffer() is False:
            raise RuntimeError("mark tap is still capturing")
        self.state = "stopped"
        try:
            self._concat()
            self._save_meta()
        except Exception:
            # Process exit is known, but a failed merge/sidecar means the
            # output cannot be offered to the upload worker as verified.
            self.finalize_failed = True
            raise
        return self.final_path

    def discard(self):
        """Throw the whole session away: kill capture, delete every artefact.

        The official client's cancel path (OFFICIAL-UI-SPEC.md §6.3): the same
        stopRecording RPC as a normal stop, distinguished by scene: "cancel",
        which the spec infers means "discard rather than upload". Here that
        means never running _concat() at all -- SIGKILL, not SIGINT, because
        there is no reason to pay for a clean Opus finalize on a file about to
        be deleted.

        Must remove all three artefacts a session can leave on disk, or
        library.scan() still finds one and shows a ghost entry in "Envios
        recentes": the segment dir (loose .opus parts, present before the
        first stop/pause concat), final_path (the concatenated .opus, present
        after at least one pause/resume cycle already merged), and the sidecar
        .json _save_meta() has been writing since __init__. Both of the first
        two are checked rather than assumed present -- which artefacts exist
        depends on how far the session got before being discarded.
        """
        if self.proc:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self.proc = None
        self._stop_mark_buffer()
        self.state = "discarded"
        for seg in self.segments:
            try:
                seg.unlink()
            except OSError:
                pass
        if self.segdir.is_dir():
            for extra in self.segdir.glob("*"):
                try:
                    extra.unlink()
                except OSError:
                    pass
            try:
                self.segdir.rmdir()
            except OSError:
                pass
        try:
            self.final_path.unlink()
        except OSError:
            pass
        try:
            self.meta_path.unlink()
        except OSError:
            pass

    def _concat(self):
        segs = [s for s in self.segments if s.exists() and s.stat().st_size > 0]
        if self._merged:
            # This session already merged: its segments are gone because they
            # were consumed, not because they were lost. Counting them would
            # report a phantom gap in a recording that is intact. Keyed on the
            # merge actually having happened, never on final_path existing --
            # an explicit `name=` is not de-duplicated (see __init__), so that
            # file can belong to an earlier session, and treating it as ours
            # would hand back someone else's recording as if it were this one.
            return self.final_path
        # A segment that was started but left no usable file is audio the user
        # watched the timer count and will not get back, and nothing surfaced an
        # error: ffmpeg writes the Ogg only at a clean exit, so an in-progress
        # segment is 0 bytes for its whole duration and the SIGKILL fallback in
        # pause()/stop() leaves it that way; a spawn that died instantly (bad
        # output path, no such encoder) leaves nothing either, and start() sets
        # state "recording" without ever looking. Dropping those silently made a
        # 12 s session produce a 5.9 s upload -- measured, both halves -21.1 dB.
        # Record it as its own flag rather than concat_failed: the segments that
        # DID survive must still be merged and uploaded (Principle II -- raising
        # concat_failed here would upload nothing at all), so this only reports.
        self.segments_lost = len(self.segments) - len(segs)
        if self.segments_lost:
            with open(LOG_DIR / f"{self.session}.log", "a") as lf:
                lf.write(f"\n[{datetime.now()}] SEGMENT LOST {self.segments_lost} of "
                         f"{len(self.segments)} produced no usable file; "
                         f"the recording is short by that much\n")
        if not segs:
            # Nothing this session recorded survived, so there is nothing to
            # merge -- but final_path may already hold an EARLIER session's
            # audio, because an explicit `name=` skips the de-duplication in
            # __init__. Returning quietly left that file in place looking like
            # this session's output, and _do_upload reads the attribute rather
            # than this return value, so it uploaded the wrong meeting under
            # the current session's name with nothing anywhere saying so.
            # Raise the same flag the concat-failure path uses: it already
            # means "do not upload, the audio is not where you think it is",
            # and it deletes nothing -- the stale .opus and any segments stay
            # on disk (Principle II).
            self.concat_failed = True
            return None
        if len(segs) == 1:
            os.replace(segs[0], self.final_path)
        else:
            listf = self.segdir / "concat.txt"
            listf.write_text("".join(f"file '{s}'\n" for s in segs))
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                 "-safe", "0", "-i", str(listf), "-c", "copy", str(self.final_path)],
                capture_output=True,
            )
            # Never delete the segments unless the concat demonstrably worked:
            # they are the only copy of the audio (Principle II). Two failures
            # to catch, and neither shows up as a non-zero exit on its own:
            #   - a write failure (full disk, bad path) exits non-zero, no output
            #   - a corrupt segment is SKIPPED and ffmpeg still exits 0, leaving
            #     a final file that silently lost part of the recording
            err = (r.stderr or b"").decode("utf-8", "replace")
            skipped = "Impossible to open" in err
            if r.returncode != 0 or not self.final_path.exists() or skipped:
                with open(LOG_DIR / f"{self.session}.log", "a") as lf:
                    lf.write(f"\n[{datetime.now()}] CONCAT FAILED rc={r.returncode} "
                             f"skipped={skipped}\n{err}\n"
                             f"segments kept in {self.segdir}\n")
                self.concat_failed = True
                return None
        # Both branches reach here only on a merge that demonstrably worked;
        # every failure above returned early.
        self._merged = True
        # limpar segmentos
        try:
            for s in self.segments:
                if s.exists():
                    s.unlink()
            for extra in self.segdir.glob("*"):
                extra.unlink()
            self.segdir.rmdir()
        except OSError:
            pass
        return self.final_path

    def _save_meta(self):
        meta = {
            "session": self.session,
            "created": self.ts,
            "mode": self.mode,
            "mic": self.mic,
            "state": self.state,
            "elapsed_s": self.elapsed(),
            "final_path": str(self.final_path),
            "segments_lost": self.segments_lost,
            "screenshots": self.screenshots,
            "notes": self.notes,
            # getattr, like "source" and "upload" below: _save_meta is reached
            # from paths that build a Recorder without __init__, and a hard
            # reference to a newly-added attribute turns a missing key into an
            # AttributeError that aborts the sidecar write -- losing the notes
            # the user typed to add a field nobody read yet.
            "flags": getattr(self, "flags", []),
            "source": getattr(self, "_last_src", None),
        }
        upload = getattr(self, "upload", None)
        if upload is not None:
            meta["upload"] = upload
        write_json(self.meta_path, meta)

    def set_upload(self, **fields):
        """Record how the upload went, so the outcome survives the process.

        The sidecar is the only record that outlives the notification: a failed
        upload otherwise leaves a .opus in recordings/ with nothing on disk
        saying it was never sent, and the popup is gone by morning.
        """
        fields.setdefault("at", datetime.now().isoformat())
        self.upload = dict(getattr(self, "upload", None) or {}, **fields)
        self._save_meta()


if __name__ == "__main__":
    import sys
    # smoke test: grava N segundos do sistema
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    mode = sys.argv[2] if len(sys.argv) > 2 else "system"
    print(f"Sinks: {list_sinks()}")
    print(f"Default sink: {default_sink()}")
    print(f"Playing sink: {playing_sink()}")
    r = Recorder(mode=mode, name="smoketest")
    # Exercise pause/resume: without it this check cannot see anything about
    # resume behaviour, which is where the source is re-used and the segments
    # are produced. Split the requested seconds across the two halves so the
    # recording still lasts the `secs` the caller asked for.
    first = max(secs // 2, 1)
    # Both halves floor at 1 s, so `secs` below 2 records 2 s rather than less:
    # a sub-second half does not meaningfully exercise pause/resume.
    second = max(secs - first, 1)
    print(f"Gravando {first}s em modo '{mode}'...")
    r.start()
    time.sleep(first)
    print("Pausando...")
    r.pause()
    time.sleep(1)
    print(f"Retomando por {second}s...")
    r.resume()
    time.sleep(second)
    # Snapshot the ring BEFORE stop(), which tears the tap down -- this is the
    # moment the flag button will fire at. Dump it beside the recording so its
    # volume can be measured the same way.
    mark_out = None
    if r.mark_buffer is not None:
        pcm = r.mark_buffer.snapshot()
        print(f"Ring: {len(pcm)} bytes = {len(pcm) / (MARK_RATE * 2):.2f}s "
              f"(teto {MARK_SECONDS}s)")
        mark_out = r.mark_buffer.dump_ogg(REC_DIR / f"{r.session}_mark.ogg")
    else:
        print("Ring: sem tap (a gravacao segue normal)")
    out = r.stop()
    # Print the source the Recorder actually captured from, not a fresh probe:
    # a third independent call to pick_system_monitor() can answer differently
    # from what the segments were recorded with, so it is not evidence.
    print(f"Fonte usada: {getattr(r, '_last_src', None)}")
    print(f"Segmentos: {len(r.segments)}  perdidos: {r.segments_lost}  "
          f"concat_failed: {r.concat_failed}")
    if out and out.exists():
        sz = out.stat().st_size
        print(f"OK -> {out} ({sz} bytes)")
        # Size and exit code say nothing about audio: a silent Opus is a
        # plausible-looking file. Measure it.
        print(f"Verifique o volume:  ffmpeg -i {out} -af volumedetect -f null -")
    else:
        print("FALHOU: sem arquivo final")
    if mark_out:
        print(f"Mark -> {mark_out} ({mark_out.stat().st_size} bytes)")
        print(f"Verifique o volume:  ffmpeg -i {mark_out} -af volumedetect -f null -")
    else:
        print("Mark: nenhum (ring vazio ou tap indisponivel)")
