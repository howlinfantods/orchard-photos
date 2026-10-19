# How Orchard works

Orchard has two parts: a Python server and a React web page. The server scans a folder, builds an index, and runs the search model. The page asks the server for images and shows them in the grid, viewer and map. Both run on your computer, and the server only ever reads your photos.

```text
your folder
  -> scanner: find image files, hash them, read EXIF, make thumbnails
  -> SQLite index
  -> SigLIP 2: turn each thumbnail into a vector
  -> near-duplicate groups, map layout
  -> HTTP API on 127.0.0.1
  -> web page
```

## Scanning

The scanner walks the whole folder and finds every image file. It counts RAW photos and videos too, which it can't read, so Info can say they aren't shown. For each new or changed file, it:

1. Hashes the contents with SHA-256.
2. Reads the width, height, camera and date taken from EXIF.
3. Draws a 720 px WebP thumbnail.

Images are stored by their hash, so identical files share one entry, and a renamed or moved file keeps its thumbnail and search data. A file whose size and modification time haven't changed is skipped, so rescans are fast. Files that have disappeared are removed from the index, along with any thumbnails no file uses.

Large images are decoded in turn, within a shared memory budget, and shrunk before they're turned upright, so a folder of huge PNGs or TIFFs doesn't run out of memory. Images over 300 megapixels are refused.

On a Mac, the scanner can read the Photos library instead of a folder. macOS keeps the library's files closed to other apps, so `server/photos.py` asks Photos for them through PhotoKit, with the user's permission. It lists every photo with its asset id, original file name and modification date, and hands over each new or edited photo's current version, which the scanner hashes, reads and draws like a file. A photo whose modification date hasn't changed isn't read again.

When ffmpeg and ffprobe are installed, videos are indexed too, as items of their own. A video's id is the SHA-256 of its size and three 4 MiB samples, from the start, middle and end, since hashing whole files of several GB would take hours. ffprobe gives its duration, displayed size, codec, creation time and camera, and a first thumbnail is drawn from its middle. Preparing it for search then decodes only its keyframes, at most one a second, scaled to fit 384 px, and embeds each with the image encoder. Consecutive frames with a cosine similarity of 0.95 or more form one segment, which keeps its sharpest frame and that frame's vector. The frame nearest the mean vector becomes the video's grid thumbnail. A video scores in a search as its best segment, and the result says where in the video that is, so the viewer plays the original from there. Sony S-Log3 footage is converted to Rec. 709 first with the .cube file named by `ORCHARD_LOG_LUT`; without one it is indexed anyway and marked.

The server rescans the open folder every minute. A folder that's slow to list, like one on a network drive, is rechecked less often: the wait is at least 20 times as long as the last listing took.

## RAW photos

Nearly every camera stores a large JPEG preview inside each RAW file, rendered with the camera's own settings.
`server/raw.py` extracts it with [rawpy](https://github.com/letmaik/rawpy) (LibRaw), which takes a few milliseconds
where developing the sensor data takes a second or more, and reads the EXIF from the RAW file's TIFF header. On a
Sony A1 file the preview is full size. A RAW file without a preview is developed at half size instead. Without
rawpy installed, RAW files are counted as skipped, as before.

## Film looks

`server/film.py` loads the engine library of an installed [Fotufilm](https://fotufilm.com) through the C interface
in its `fotufilm.h`, so a look is developed inside the server process rather than by a program started per photo.
One engine serves the app, and renders on it run one at a time. The engine decodes the original itself, so a RAW
file is developed from its sensor data. The last three decoded originals stay open, since trying several looks on one
photo is the common case. A look developed for viewing is rounded up to 512, 1024, 2048 or 4096 pixels and kept in
`film/`, named by the look, the size and the engine build, so a repeat is a file read and a new Fotufilm develops
afresh. Exports develop at full size, one photo at a time, in the background.

The engine delivers Display P3, and Orchard saves it with a Display P3 profile, which Fotufilm's
`tools/generate-web-color-profiles.py` generates from the public definition. On Linux the engine develops on CUDA, or on
Vulkan with `FOTUFILM_GPU_DEVICE=vulkan`, which also runs on Mesa's software Vulkan device when no GPU is free.

Measured on a Ryzen 9900X with software Vulkan (no GPU): a 50-megapixel Sony ARW took 2.5 s for its first look at
2048 pixels, including decoding, and 0.37 s for the next stock. Exporting it and two smaller photos (12 and 5 megapixels) at full size took 26 s.

## Visual search

Orchard uses SigLIP 2 ([`google/siglip2-base-patch16-256`](https://huggingface.co/google/siglip2-base-patch16-256)), a model trained to turn both images and text into vectors. An image and a sentence that describe the same thing get vectors that point in a similar direction. It reads descriptions in many languages.

After a scan, the server runs each new thumbnail through the model and stores its vector, newest images first. To search, it turns your description, or your example image, into a vector, and ranks every image by cosine similarity to it. Even with tens of thousands of images, this is one quick matrix multiplication, so there's no separate search index.

The model runs with ONNX Runtime, from an ONNX export of the same weights ([`onnx-community/siglip2-base-patch16-256-ONNX`](https://huggingface.co/onnx-community/siglip2-base-patch16-256-ONNX)). The image half is full precision. The text half is the 8-bit copy, which is a quarter of the size and ranks searches just as well. Both are downloaded once from Hugging Face at a pinned revision, and shared by every folder.

Where it can, Orchard runs the image half on the GPU:

- **Apple Silicon Macs** use Core ML, about 7 times faster than the CPU. Core ML can't compile the published file well, so Orchard downloads a 16-bit copy rewritten for it from [`ChaseLean/siglip2-base-patch16-256-coreml-fp16`](https://huggingface.co/ChaseLean/siglip2-base-patch16-256-coreml-fp16), and checks its SHA-256. `scripts/build-coreml-model` builds that file, and the math is unchanged.
- **Windows on x64** uses DirectML, which works with any DirectX 12 GPU, from NVIDIA, AMD or Intel. It runs the same file as the CPU.
- **Everything else**, including Windows on Arm and Linux, uses the CPU. On Linux, installing `onnxruntime-gpu` in place of `onnxruntime` runs it on an NVIDIA GPU through CUDA.

Before using Core ML or DirectML, Orchard runs a fixed test image and compares its vector with the CPU's, stored in `server/parity-vector.npy`. If the GPU fails to load the model or its vector is off, the CPU runs it instead, and on a Mac, downloads the full-precision file first. The text half runs on the CPU, or on CUDA when `onnxruntime-gpu` is installed. On the CPU, a search takes a few tens of milliseconds.

Vectors are stored with the name of the model that made them. Vectors from any other model are deleted when a folder opens, along with the map and stacks made from them, since vectors from two models can't be compared.

## Near-duplicates

Bursts, edits and resized copies are different files, so they have different hashes, but their vectors are almost the same. Orchard groups images whose vectors have a cosine similarity of 0.9775 or more. Versions of one design, like an illustration in two colours, can score nearly that high, so the cut sits close to 1.

Each group forms around one centre, and every member must match that centre. This stops a slow series, like a plant photographed every day, from chaining into one long group. Crops differ more than resized copies, so Orchard also groups files in the same folder whose names differ only by a size or copy suffix, like `photo-800x400.jpg` and `photo (1).jpg`, as long as their cosine similarity is at least 0.72.

Screenshots, renders and web graphics have no camera details, and different screens of one app or design can score as high as copies. So when a pair has such an image, and both have the same shape, they also have to look alike pixel by pixel, compared in grey at 64 × 64. Copies pass, and different screens don't.

The image that fronts each group is the sharpest one, measured by the variance of the Laplacian. Between copies of one picture, which are about equally sharp, the larger one wins.

## The map

The map uses UMAP to squeeze the vectors down to two dimensions, so similar images land close together. UMAP links each image to its 15 nearest neighbours, then arranges the images so linked ones pull together while random pairs push apart. Orchard has its own short version in NumPy, which saves several hundred MB of dependencies. In folders of more than 10,000 images, it finds neighbours by sorting the images into clusters and comparing each one only with the clusters around it, which keeps the layout to seconds.

Positions are saved. When new images arrive, each is placed at the weighted centre of its nearest neighbours, so the map stays put. Orchard fits the layout again only once new images make up more than a fifth of the map.

To draw thousands of images quickly, the server packs small icons into sheets of 1,024 icons each, so the page loads a handful of sheets instead of one image per request. The map shows at most the newest 25,000 images that match the filters, and new images get their icon slots newest first, so those images share the first sheets.

## The server

The server is a small HTTP server from Python's standard library. It serves the built page from `dist/` and a JSON API under `/api/`. It only listens on `127.0.0.1`, and accepts requests only from its own page and from programs on the computer, so a web page you visit can't read your photos through it. The Host must be 127.0.0.1, localhost or [::1] on the server's port, which stops DNS rebinding. The `Origin` and `Sec-Fetch-Site` headers turn away every other page, even one on another local port, and a POST must be JSON, which a web form can't send. The one exception is an image search, which sends the image itself. Every response tells the browser not to frame the page or guess content types, and the page runs only its own scripts. The server logs each request it refuses as a `Refused` line.

Only one folder is open at a time. Opening another folder closes the current one and stops its background work.

## Where data lives

The cache lives in Orchard's [data folder](using-orchard.md#where-orchard-keeps-its-data), which you can change with `--data-dir`.

| Path | Contents |
| --- | --- |
| `models/` | The SigLIP 2 weights, shared by all folders. |
| `libraries/<id>/` | One folder's cache: its SQLite index, thumbnails, previews, vectors, map positions and icon sheets. `folder.txt` names the folder it belongs to. The Photos library's cache also has `exports/`, the photos written out to view or download, which go with each scan. |
| `recent.json` | The recently opened folders. |

Deleting a `libraries/<id>` folder clears that folder's cache. Orchard rebuilds it the next time you open the folder.
