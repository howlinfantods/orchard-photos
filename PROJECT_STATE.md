# Orchard (fork) — state
updated: 2026-10-19 12:40 · by: claude-code@3090 · checkout: 3090:/home/bryan/cairn-work/orchard-photos · main @ dfbfe70 (upstream chaseleantj/orchard-photos)
goal: Bryan's local photo+video search: Orchard plus video, RAW, Lightroom/Photos catalogs and Fotufilm film looks.

## Next
on 3090: review lane reports, merge cairn/video and cairn/catalogs into main, rerun suite + real server run · cairn-1ak7

## Last accepted
- Oct 19: forked to howlinfantods/orchard-photos and howlinfantods/fotufilm-engine; baseline 159 tests OK, build+lint clean
- Oct 19: libfotufilm.so (AppImage v1.14-beta) via ctypes on lavapipe (CPU): 47 stocks, 368 ms @1024 px, 451 ms @2048 px after 4.5 s warm-up
- Oct 19: RAW via embedded JPEG (rawpy) + Fotufilm film looks/export in app; 167 tests OK, lint/build clean; real server: 50 MP ARW first look 2.5 s, next stock 0.37 s, cache 2 ms, 3-photo full-res export 26 s; headless Chromium viewer screenshot OK · commits 95ef247, dfbfe70

## Running or unfinished
- lane orchard-video · 3090 ~/worktrees/orchard-video (cairn/video) · card 20261019t163002z-orchard-video-532d5b · check LANE-REPORT.md there
- lane orchard-catalogs · 3090 ~/worktrees/orchard-catalogs (cairn/catalogs) · card 20261019t163002z-orchard-catalogs-ca5c17 · check LANE-REPORT.md there

## Needs Bryan
- none

## Do not repeat
- 3090 GPU is held by the Harness2 pilot: develop Fotufilm on lavapipe (VK_ICD_FILENAMES=lvp_icd.json, FOTUFILM_GPU_DEVICE=vulkan), never CUDA, until released.
- Prebuilt Linux libfotufilm has no plain-CPU path (FOTUFILM_GPU_DEVICE=cpu → "Halide engine is not linked"); set FOTUFILM_STOCKS to resources/Stocks.
- Never open a live .lrcat or Photos.sqlite: copy into Orchard's cache, open the copy read-only.

## Pointers
bead cairn-1ak7 · briefs ~/command-center/output/delegation/briefs/2026-10-19-orchard-{video,catalogs}.md · AppImage extract ~/cairn-scratch/fotufilm-appimage-v1.14 · video method sources: command-center docs/media/ANALYSIS-PREPROCESSING.md, output/reports/angels-interior-sfm-2026-09-28 (I-frame decode)
