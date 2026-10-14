# Changelog

## v0.5.0

What ships, what the repo says and what Emma does now agree.

- **One Emma.** The app no longer offers an OpenAI key, an account, pairing or
  free minutes. Onboarding asks for your ElevenLabs key and a voice, and
  nothing else. `emma.permissions check` and the boot log call this mode
  `local`.
- **One menubar icon.** Opening Emma.app shows the window of the Emma that is
  already running instead of starting a second copy.
- **Upgrades replace the source tree.** Files removed in a release no longer
  linger in `~/.emma/src`.
- **Uninstalling clears everything.** Every Keychain item Emma wrote (your
  ElevenLabs and OpenRouter keys, tokens, saved secrets) and every macOS
  permission row for both Emma bundles.
- **Leftover OpenAI keys do nothing.** A key exported by an old shell profile no
  longer hides your other credentials, opens an OpenAI session, or makes an
  OpenAI call at boot. Reminders and other unprompted lines speak in Emma's own
  voice.
- **Update notices compare releases.** Emma checks the newest release tag once a
  day and shows "vX.Y.Z disponible" in the menubar. It used to compare against
  unreleased work and called every install out of date.
- The session counters in the window count again, and the visualizer page
  connects again.
- Docs describe the shipped design: README, SECURITY (the only egress is
  ElevenLabs, with your key) and AGENTS.
- Settings that nothing read are gone. Old `.env` lines for them are ignored.
- **Logs hide anything shaped like a government ID.** Emma used to look for
  a few named national formats. If yours has a format the generic shape
  misses, add it to `~/.emma/dictionary.toml` under `[redaction] id_patterns`.
  A dashed number like 123-45-6789 is now redacted only if you add its format.
- **Screen reading takes its languages from macOS.** Spanish and English text
  is read with the variants macOS offers, instead of two fixed ones.

## v0.4.1

- Reads the ElevenLabs key from the Keychain even when an old `OPENAI_API_KEY`
  is exported in the shell.

## v0.4.0

- Emma hears with ElevenLabs Scribe v2, decides on your Mac with her own brain
  (no language model) and speaks with ElevenLabs TTS, using your key and the
  voice you choose.
