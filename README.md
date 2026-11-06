# NKPI 9.2.1 — fluid corneal replay

**Replay-only update. The working extractor is unchanged.**

This release builds on `NKPI_Oct27_Extractor_Replay_GitHub.zip` (9.2.0). It keeps the
purple bilateral calculator, original natural-eye viewer, OCR reader/delivery adapters
and frozen NKPI models. It replaces the replay slideshow with continuously changing
surface geometry and frame-by-frame video encoding.

## Upload four files

Extract the OUTER download once. Replace all four files in the same GitHub folder
that already runs your app:

    kcn_dual_calculator_app.py
    nkpi_runtime.zip
    requirements.txt
    README.md

Keep `nkpi_runtime.zip` zipped. Always replace it and the launcher together. Do not
upload an extra enclosing folder. No new repository or hidden configuration folder
is needed. Keep `kcn_dual_calculator_app.py` as the Streamlit main file and the existing
Python 3.12 target. Reboot the app and refresh. The banner should say **Build 9.2.1**.

## Use

Import two visits using the unchanged workflow, verify measurements and calculate.
Below the original 3D viewer, select **Prepare & play**. It starts continuous motion
unless the device requests reduced motion; then press Play. Controls are above the eyes.
Use Side profile / cone to inspect the outline. The x1 view is not exaggerated. The
optional x3/x5/x10 choices magnify only the displayed change, with permanent labels.

**Save smooth video** creates a complete 30 fps WebM in supported browsers and opens it
in an on-page video player. It builds all frames first rather than recording a slow
screen. **Save animated GIF** creates a 20 fps GIF. Both run locally and can be canceled.
The video fallback for browsers without WebP encoding has a variable recording rate.

The close-up omits most static reference anatomy. Shared supported coordinates move;
unsupported regions remain gray and fixed. Different source grids or missing spatial
pairs prevent animation rather than inventing a cornea from scalar inputs.

## What is and is not claimed

The movie illustrates a transition between two reconstructed endpoints. Intermediate
frames are NOT measured scans, biological predictions or proof of progression.
Magnified change is not actual anatomy. Reconstruction remains approximate and may
use thickness-derived inner geometry. Review treatment history and original reports.
This remains a research prototype and is not a treatment recommendation.

55 Python tests, 26 JavaScript test scripts and 24 synthetic browser checks passed.
The actual browser-exported test movie decoded as 120 frames at 30 fps over four seconds;
the GIF decoded as 80 frames at 20 fps. The extractor, OCR delivery code, primary viewer
and model artifact hashes match the base package. Details and limitations are in
`docs/FLUID_REPLAY.md` inside the runtime. No patient ZIP, clinic device or live
Streamlit deployment was tested for this release. No improved clinical accuracy or
clinic frame-rate guarantee is claimed. Keep identifiable data out of public GitHub.
