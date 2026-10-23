# docs/media

The images in the README and on the project page. They show demo data: four invented projects, people and
conversations built on the end-to-end test harness (`tests/piper-e2e/run.py`) with a fake hole. Nothing real
is started or read.

| File | Made by |
| --- | --- |
| `queue.png`, `plan.png`, `conversation.png`, `files.png`, `git.png`, `grid.png`, `security.png`, `demo.gif` | `showcase.py` + `capture.sh` |
| `banner.svg`, `social.svg` | `make_banner.py` (the GUI's icon and dark theme tokens) |
| `social.png` | `social.svg` rendered at 1280×640, the repository's social preview |

## Regenerate

Needs agent-browser and ffmpeg. Start the showcase in the test sandbox, then capture it:

```sh
python3 scripts/piper-sandbox.py -- python3 docs/media/showcase.py --port 17991   # prints SHOWCASE <url>
sh docs/media/capture.sh '<url>' docs/media                                        # in another shell
python3 docs/media/make_banner.py
```

`capture.sh` uses a fresh headless browser at 1440×860, allows the waiting push after the first shot, and
builds the GIF from the shots (2.4 s per screen, 0.4 s crossfade). Look at every image before committing it.
The project page (`site/index.html`) is deployed by `.github/workflows/pages.yml`, which copies these files
to `media/`.
