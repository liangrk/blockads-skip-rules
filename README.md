# blockads-skip-rules

Skip-button rules for the BlockAds accessibility ad-skip feature.

Downstream client: [liangrk/self-dns](https://github.com/liangrk/self-dns) —
downloaded via the same CDN mirrors as the DNS filter lists.

## Sources & update flow

```
gkd-kit/subscription (upstream, daily)  ─┐
manual/manual-rules.json (human)        ─┼─→ scripts/fetch_gkd.py → dist/skip-rules.json
                                        ┘         (three-layer safety filter)
```

Daily GitHub Actions sync (`update.yml`) fetches the upstream GKD
subscription and converts ONLY the rules that pass all safety filters;
`manual/manual-rules.json` holds human-confirmed app-specific rules
(merged under the same validation).

## Safety model (fail-closed)

Upstream GKD rules are community-contributed. A malicious rule could make
the client tap arbitrary UI (e.g. a payment confirm button). Protection:

1. **Action filter** — only `click` actions survive; `back`, `longClick`,
   gestures, `openUrl`/`openApp` are dropped.
2. **Structural filter** — only flat single-expression selectors are
   convertible; parent/child/sibling combinators are dropped (cannot be
   expressed safely in our simplified format).
3. **Pattern whitelist** — text/desc patterns must contain a skip term
   (跳过/跳過/skip); vid/id patterns must contain a safe id hint
   (skip/count/down/close/jump) or end with `tt_splash_skip_btn`.
   Unknown patterns are dangerous by default → dropped.
4. **Caps** — ≤1000 apps, ≤20 patterns per app, ≤2MB dist file; over-cap
   aborts the build (nothing is emitted).
5. **Client re-validation** — the Android client re-checks every rule
   against the same word lists before use (defense in depth): even a
   compromised dist cannot make the client tap arbitrary UI.

Result on current upstream: ~37K rules → 3 app entries pass (the
valuable general logic lives in the client's built-in global matcher,
which mirrors the GKD global splash group).

## Manual rules

Edit `manual/manual-rules.json`:

```json
{"pkg": "com.example.app", "texts": ["跳过"], "vids": ["skip_btn"],
 "idSuffixes": ["tt_splash_skip_btn"], "activityIds": ["com.example.Splash"]}
```

The converter re-validates manual rules against the same word lists —
a manual rule violating them is rejected at build time.
