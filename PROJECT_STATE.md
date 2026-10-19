# Orchard (fork) — state
updated: 2026-10-19 12:54 · by: claude-code@3090 · checkout: 3090:/home/bryan/cairn-work/orchard-photos · main @ 89f2c03 (upstream chaseleantj/orchard-photos)
goal: Bryan's local photo+video search: Orchard plus video, RAW, Lightroom/Photos catalogs and Fotufilm film looks.

## Next
on M4: install Fotufilm, run Orchard on FilmFest2026.lrcat with real originals; judge looks by eye; measure Metal speed (main not pushed; push needs Bryan's yes) · cairn-1ak7

## Last accepted
- Oct 19: forked to howlinfantods/orchard-photos and howlinfantods/fotufilm-engine; baseline 159 tests OK, build+lint clean
- Oct 19: libfotufilm.so (AppImage v1.14-beta) via ctypes on lavapipe (CPU): 47 stocks, 368 ms @1024 px, 451 ms @2048 px after 4.5 s warm-up
- Oct 19: RAW via embedded JPEG (rawpy) + Fotufilm film looks/export in app; 167 tests OK, lint/build clean; real server: 50 MP ARW first look 2.5 s, next stock 0.37 s, cache 2 ms, 3-photo full-res export 26 s; headless Chromium viewer screenshot OK · commits 95ef247, dfbfe70

- Oct 19: Lightroom catalogs + Photos bundles merged (496c780) and checked on real copies: FilmFest2026.lrcat (9,701 images; 30 A7 V ARW + 40 offline previews shown; ratings/keyword/collection filters right; copy unchanged), M4 Photos.sqlite schema + album join. Fixes: LrC 15 per-size previews, A7 V ARW via embedded JPEG, film cache key · b56787f
- Oct 19: video indexing merged (89f2c03): keyframes ≤1/s → segments, search returns t, player seeks; combined real run with RAW+catalogs OK; 234 tests · ~/command-center/output/reports/orchard-fork-2026-10-19/REPORT.md
## Running or unfinished
- none (worktrees ~/worktrees/orchard-{video,catalogs} merged; removable)

## Needs Bryan
- Say whether to push main to howlinfantods/orchard-photos · cairn-1ak7

## Do not repeat
- 3090 GPU is held by the Harness2 pilot: develop Fotufilm on lavapipe (VK_ICD_FILENAMES=lvp_icd.json, FOTUFILM_GPU_DEVICE=vulkan), never CUDA, until released.
- Prebuilt Linux libfotufilm has no plain-CPU path (FOTUFILM_GPU_DEVICE=cpu → "Halide engine is not linked"); set FOTUFILM_STOCKS to resources/Stocks.
- Never open a live .lrcat or Photos.sqlite: copy into Orchard's cache, open the copy read-only.

## Pointers
bead cairn-1ak7 · briefs ~/command-center/output/delegation/briefs/2026-10-19-orchard-{video,catalogs}.md · AppImage extract ~/cairn-scratch/fotufilm-appimage-v1.14 · video method sources: command-center docs/media/ANALYSIS-PREPROCESSING.md, output/reports/angels-interior-sfm-2026-09-28 (I-frame decode)
