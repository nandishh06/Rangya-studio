# Effects pack

The 42 face-tracking effects in `scripts/fx.py` are from
[claude-video-effects](https://github.com/Vibha-Ramprakash/claude-video-effects) by Vibha Ramprakash
([@vibha.in.progress](https://instagram.com/vibha.in.progress)), MIT licence (`LICENSE`), vendored at commit
d675cff with the optional `fx_private.py` exec hook removed. Rangya also removed every built-in demo text
(the author's name/handle, "THE AI ISSUE", "Edited by Claude", fake headlines, stats and checklist steps,
the made-up news paragraph in match-cut): on-screen text now only comes from the user's speech or typing,
and the "full" glass-widgets look (fixed demo widgets) is disabled. Fonts and models keep their own licences
(`fonts/*.txt`, `models/NOTICE.md`). Rangya Studio calls it from `studio/engine/effects.py`.
