# `earl.web` — the browser demo

    python3 scripts/serve.py            # http://127.0.0.1:8000
    python3 scripts/serve.py --ngrok    # + a public URL
    python3 scripts/serve.py --open     # launch a browser too

The command-line demo (`scripts/demo.py`) tells the story in six acts. This
tells it by handing someone the controls.

## Three tabs

**Change bench.** Compose a CAD change — resize a member, delete one, add or
move a point load, edit the `designLoad` or `barArea` variable — and the whole
pipeline runs on it: the fixed BFS walk (hop rings, so you can see what the
change reaches), the PyNite solve (every member, worst first, before and
after), the Biject verdict, the rendered truss, and the change notice an
engineer would actually receive. Above the fold is the line that matters:
*the change names m7; the member that fails is m5, which nobody edited.*

**The part a model cannot promise.** On any escalated run there is a button
marked **Approve it anyway**. It takes the Decision that run really produced,
sets `outcome = APPROVED`, calls `Decision.validate()`, and prints what comes
back — a `ValueError` naming the member and its safety factor. That is Act 4
of the terminal demo, on a button, live, on the judge's own change.

**The eval.** The recorded Sprint 5A scoreboard and the consistency run,
including the part where the baseline tied us. The page says so.

## What it is built on

| Layer | File | Owns |
|---|---|---|
| domain | `api.py` | validation, the run, the shaped JSON, the tamper |
| HTTP | `app.py` | routing, limits, headers, static files |
| page | `static/` | one HTML file, one CSS file, one JS file, a favicon |
| CLI | `../../scripts/serve.py` | binding, the ngrok tunnel, the banner |

`http.server`, not a framework. `requirements.txt` has three entries and the
demo has to survive a `pip install` on a strange machine ten minutes before
we present; a router this small is not worth a fourth dependency. The cost is
worth stating plainly: `ThreadingHTTPServer` is a development server, fine for
a handful of judges behind a tunnel, and not what you would leave facing the
internet for a week.

## The public URL is the threat model

`--ngrok` means anyone with the link can drive it, so:

- **Static files come from a fixed list** (`_STATIC`), not from the request
  path. There are five entries; anything else is a 404 before the filesystem
  is touched, so there is no traversal to get wrong. `tests/test_web.py`
  fires nine spellings of `../.env` at it, including percent- and
  backslash-encoded ones, on a raw socket so nothing normalises them first.
- **A request composes a change, never a program.** Member ids, node ids and
  load ids are checked by membership in the base design's own vocabulary;
  every number is finite and inside a stated range; the scenario id reaching
  a branch name is generated here, never supplied.
- **Nothing is written, sent, or merged.** `run_pipeline` is called with no
  SkyCiv client and no Gmail sender, and no handler opens a file for writing.
- **No model is called.** A run is a walk and a solve. The eval numbers come
  from `demo/recordings`, read once and cached.
- **Bodies are capped** at 16 KB, runs are rate-limited per client, and four
  solves run at a time.
- **CSP is `self` with no `unsafe-inline`**, which the page can afford
  because its CSS and JS are separate files. The one piece of markup injected
  into the DOM is the truss SVG, which our own renderer escaped; everything
  else is written with `textContent`.
- **No CORS header is offered.** The page and the API are the same origin;
  advertising `Access-Control-Allow-Origin` would let any site drive a
  tunnelled server from a visitor's browser.

## The one thing you can do to it that looks like a hole

You can raise the safety-factor threshold from the form. You can also try to
lower it — and the request reaches `gate.resolve_threshold`, the single place
that floor lives, and is refused there with the same message a misconfigured
`.env` would get:

    safety-factor threshold 0.5 is below the floor 1.0; SF < 1.0 can never
    be auto-approved (plan.md), so this floor lives in code

The site renders that as the guarantee working, because it is. It is
deliberately *not* re-implemented as form validation in this package: a rule
enforced in two places is a rule that can disagree with itself.
