# Noisy-speech set (#144) - SIMULATED

Offline, seeded (`python -m core.eval.noisy --seed 144`), writes `report.json`. No network, no wall clock.

Runs offline now: 40 synthetic utterances (13 hi, 13 kn, 14 Hinglish, 6 with amounts, 3 in spoken words), a synthetic speech-like carrier mixed with synthetic shop noise at 20/10/5 dB SNR (achieved SNR measured per clip, error below 0.05 dB), a seeded ASR-error channel (word drop, letter confusion, fillers, digit and number-word confusion), intent accuracy per SNR, ask-once rate, and misheard actions executed through the real read-back gate (0 required).

Needs real audio and a live key (NOT done, NOT claimed): real busy-place recordings, real recogniser accuracy on them, whether `language_probability` is a usable confidence signal, an open noise corpus with licence check. The channel rates in `WORD_ERROR_RATE` are assumptions, so the accuracy numbers describe the stand-in, not Sarvam. Hindi and Kannada wording: native review expected.
