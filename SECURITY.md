# Security policy

Plaud Linux is an independent, private review copy. It uses an existing Plaud
account and an observed desktop service contract; Plaud and Omarchy do not
endorse it. No public security reporting channel or response-time commitment
has been established.

## Report privately

Contact the repository owner through your existing private channel. Do not
open a public issue containing a vulnerability, account data or recording.
Describe the affected revision, component, impact and a minimal reproduction
with fictional data. Do not include passwords, tokens, OAuth codes, signed
upload URLs, raw authentication replies, personal screenshots or audio.
Only test systems and accounts you own or are authorized to test.

For a vulnerability in Omarchy itself, use
[Omarchy's security policy](https://github.com/omacom/omarchy/blob/quattro/.github/SECURITY.md).
Do not send Plaud account data with an unrelated upstream report.

## Boundaries and limitations

- Recording requires an explicit action and a visible indicator. Cloud or
  annotation failures must not discard recorded audio. Invoked features need
  microphone, system-audio and screenshot access.
- Runtime data stays outside Git under the configured Plaud data root. Login
  state is written with private permissions. Machine-derived encryption does
  **not** defend against another process running as the same user.
- The installer, autostart toggle and launcher reject common unsafe pathname
  substitutions and bound helper execution. If a diagnostic log destination is
  unsafe, the launcher can proceed without writing that log. Cleanup skips
  unsafe derivative files and preserves recordings. These measures are scoped
  protections, not a sandbox or a claim of full runtime security review.
- Regional API redirects use an allowlist; workspace session recovery is
  bounded. If the user login is invalid, reconnecting is still required. The
  custom picker loads official remote Plaud Web content, whose behavior can
  change independently of this client.
- Protect the desktop account, storage and backups. Installing the app does
  not configure a firewall or disk encryption. Automated checks with fictional
  credentials cannot establish real-account safety or future service behavior.

The bundled Plaud-derived artwork and traced icon shapes have unresolved
redistribution rights. This private copy does not grant public distribution
rights; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Review secrets,
media and licenses before any separate publication decision. Removing a file
in a later commit does not remove it from earlier Git history.
