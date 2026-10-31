# StoryCraft


<img width="1907" height="867" alt="sc1" src="https://github.com/user-attachments/assets/829ab031-bae7-443e-9d09-789a2ea07101" />

<img width="1916" height="867" alt="sc2" src="https://github.com/user-attachments/assets/24bb5188-668a-4111-9db9-bd08a79fbad3" />


**Turn a folder of scene images and one narration script into a finished, narrated video,
automatically.**

StoryCraft is a small app that runs on your own Windows PC. You give it your story's images in
order and paste the narration. It speaks the narration with **Kokoro** (a free text-to-speech voice
running on your PC), times every scene to the speech, adds moving camera shots and burned-in
subtitles, and exports a vertical 9:16 MP4 (1080 × 1920) ready for YouTube Shorts, Facebook Reels
and TikTok.

No editing timeline and no accounts. There are no paid APIs and no cloud: everything is processed
locally and works offline once it's set up.

---

## How it works

```
images + script ──► split script into scenes ──► Kokoro speaks each scene
                                                        │
                    MP4 ◄── FFmpeg renders ◄── scene length = length of its narration
                             (motion, transitions,      subtitles timed to the spoken words
                              subtitles, watermark)
```

1. **Scenes**: each image is one scene, shown in order (`scene_2` comes before `scene_10`).
2. **Script → scenes**: paste the whole story once. If it has labels like `SCENE 1`, each section goes
   to its numbered scene. If not, StoryCraft divides it across your images at paragraph and
   sentence breaks, and you review and adjust the split before applying it.
3. **Narration**: Kokoro reads each scene. StoryCraft measures the real audio, so every image stays
   on screen exactly as long as its narration, plus a short pause. You never type durations.
4. **Subtitles**: Kokoro reports when every word is spoken, so short captions (about 3–7 words)
   appear and disappear in sync with the voice.
5. **Video**: FFmpeg animates each image with a slow zoom or pan, shows it whole without cropping,
   adds cuts or fades, burns in subtitles and your watermark, and encodes the MP4 (H.264 / AAC).

Narration is cached per sentence. Changing images, subtitle style, motion or timing never makes
Kokoro speak again; editing a sentence only re-speaks that sentence.

---

## What you need

| | |
|---|---|
| **Windows 10 / 11** | |
| **Python 3.10 or newer** | [python.org/downloads](https://www.python.org/downloads/) (keep the default options; tkinter is included) |
| **Kokoro**, installed in that Python | Either kind works, see below |
| **FFmpeg** | In a terminal: `winget install Gyan.FFmpeg` |
| **Codex** (optional, for scene images) | ChatGPT Codex app or `npm i -g @openai/codex`, then `codex login` → *Sign in with ChatGPT* |
| **Git** (to download / update) | [git-scm.com](https://git-scm.com/), or use GitHub's *Download ZIP* button |

**Kokoro**: StoryCraft uses the Kokoro you already have. It does not download its own.

- **hexgrad/Kokoro-82M (PyTorch)**: installed with `pip install kokoro` (which brings `torch`), with
  the model downloaded at least once (it lands in the Hugging Face cache), or in a folder containing
  `kokoro-v1_0.pth`, `config.json` and `voices\*.pt`.
- **kokoro-onnx**: `pip install kokoro-onnx` plus the files `kokoro-v1.0.onnx` and `voices-v1.0.bin`.

---

## Getting started (after cloning)

**1. Get the code**

```
git clone https://github.com/Yinkci/StoryCraft.git
```

(or on GitHub: **Code → Download ZIP**, then unzip it)

**2. Double-click `start.bat`** in the `StoryCraft` folder.

A black window opens. The **first start needs internet** and takes about a minute. It will:

- find the Python on your PC that has Kokoro installed;
- create StoryCraft's own environment (`.venv`) that reuses that Python's packages;
- install the few small packages StoryCraft needs (FastAPI, Uvicorn);
- find your Kokoro model and FFmpeg automatically;
- open the app in your browser at **http://127.0.0.1:8765**.

**3. Check the top-right corner** of the app. It should show **Kokoro (PyTorch) · 54 voices** (or
*Kokoro (ONNX)*) and **FFmpeg** with green dots. If so, you're ready.

From then on, just double-click `start.bat`. Keep the black window open while you work;
closing it stops StoryCraft.

### If something isn't found

- **"Setup needed" → Locate Kokoro…**: pick your model file: `kokoro-v1_0.pth` (Kokoro-82M) or
  `kokoro-v1.0.onnx`. A downloaded Kokoro-82M usually lives in
  `C:\Users\<you>\.cache\huggingface\hub\models--hexgrad--Kokoro-82M\snapshots\<id>\`.
- **"Setup needed" → Locate FFmpeg…**: install FFmpeg (`winget install Gyan.FFmpeg`) and restart
  StoryCraft, or pick `ffmpeg.exe`.
- **The black window says no Python has Kokoro**: your Kokoro is probably inside a separate
  virtual environment. Create a text file named `python_path.txt` next to `start.bat` containing
  the full path of that environment's Python, for example
  `C:\Users\you\kokoro-env\Scripts\python.exe`, then start again.
- **"Python 3.10 or newer was not found"**: install Python from python.org.

Your choices are remembered in `config.json` (created on your PC, never uploaded to GitHub).

### Updating

```
git pull
```

Then close the black window, double-click `start.bat` again, and press **Ctrl+F5** in the browser.
(The app also shows a banner when it notices its files changed and it needs a restart.)

---

## Making a video

1. **Add your images**: drag them onto the scene list (or click the drop area). PNG, JPG and WebP
   are supported. They're sorted by file name.
2. **Import full script**: paste your whole narration and click **Auto assign to scenes**.
   - *With labels* (`SCENE 1`, `Scene 01:`, `[Scene 1]`, `Scene 1 — Apartment`): each section goes
     to that scene and the labels aren't spoken. You get a warning if the count doesn't match the
     number of images.
   - *Without labels*: the story is split into one part per image, preferring paragraph and
     sentence breaks. **This is an estimate**: StoryCraft does not look at the pictures, so check
     each image against its text in the review. Use **▼ / ▲** to move a sentence to the
     neighbouring scene, or edit the text directly. The review confirms that every word was used
     exactly once, in order.
   - Nothing changes until you click **Apply**, and **Undo** is offered right after.
3. **Generate narration**: Kokoro speaks all scenes, and each scene shows its length. Use
   **▶ Narration** on a scene to hear just that one; pick the voice and speed under *Settings → Voice*
   (**Test voice** plays a sample).
4. **Preview video**: a quick half-size render with the real timing, motion, subtitles and
   transitions. Play, pause and seek it in the middle column.
5. **Export MP4**: the full-quality video goes to the output folder set under *Settings → Export*
   (default: the `exports` folder).

You can still edit any scene's text box by hand, reorder scenes by dragging **⋮⋮** (or ↑ ↓),
replace an image by clicking its thumbnail, and duplicate or delete scenes.

**Save / Open** keep a project as a `.storycraft.json` file (default folder: `projects`). Your
current work is also autosaved in the browser. Uploaded images are copied into the `media` folder;
your original files are never changed.

---

## Settings at a glance

- **Format**: always vertical 9:16 (Shorts / Reels), in 1080p or 720p (Export settings).
- **Voice**: any installed Kokoro voice, speed 0.5×–2×.
- **Subtitles**: on/off (**CC** switch under the preview), font, size, color, outline, shadow,
  bold, UPPERCASE, words per caption, and position: X, Y and caption width. You can also drag the
  caption on the preview. **✥ Adjust positions** lets you drag it on a still frame after rendering.
- **Scene images (ChatGPT)**: generate a scene's picture with your ChatGPT plan. **🎨 Image** on a
  scene opens a window with a prompt based on that scene's narration (edit it freely), **Generate**,
  then **Use this image** (Undo available). **🎨 Generate missing images** fills every scene that has
  no image, one after another. To keep your series' look:
  - **Art style = the `artstyle` folder**: put images with the look you want into the `artstyle`
    folder in StoryCraft's folder (*Settings → Scene images → Open folder*). Your best existing scenes
    work well. Up to 3 are sent with every image, rotating through all of them. Optionally add
    `artstyle\rules.txt` with your own rules, one per line (e.g. *Any blood is drawn bright pink.*).
    A flat 2D webcomic style description and a *never draw* list (no anime, no realistic humans,
    no dramatic lighting…) are built in. Images and rules in the folder stay on your PC; they're not
    uploaded to GitHub.
  - **Characters**: each with a name, one or two pictures and a short description of their look.
    The **main character** is in every scene (the "you" of the story). Others are included when the
    narration mentions their name, and you can tick or untick them per scene in the image window.
  - Every attached picture is labeled in the prompt (style example / which character), so ChatGPT
    knows what to copy. Characters are saved once for **all your projects** (`series_style.json`). This uses the **Codex app / Codex CLI signed in with
  your ChatGPT account**: no API key, and it counts against your plan's Codex limits. About a minute
  per image. Setup: install the ChatGPT Codex app (or `npm i -g @openai/codex`), run `codex login`,
  and choose *Sign in with ChatGPT*. The section shows *Connected* when it's ready.
- **Watermark**: your text (e.g. `@YourChannel`), bottom right by default. Snap it to any corner,
  edge or the center with the 3×3 grid, fine-tune with X / Y, or drag it on the preview. Size,
  opacity, font, color and outline are adjustable.
- **Motion & transitions**: static, slow zoom in/out, slow pan left/right, or random subtle movement
  (per scene or for all). Fit mode is *Contain*: the whole image is shown with black bars if
  needed, never stretched. The picture stays fixed in place and the **camera moves inside it**, like a
  Ken Burns move. During a move a thin band at the picture's edges is briefly out of view (up to
  about 7–8%). A zoom-in starts on the whole picture and a zoom-out ends on it. *Cover* fills the
  frame and crops. Transitions: cut,
  crossfade or fade through black. They always happen in the pause between scenes, so speech is
  never cut off.
- **Timing**: pause between scenes, pause between sentences, lead-in, hold at the end.
- **Export**: output folder, file name, frame rate (24/30/60), quality, and **Clear video cache**.

---

## Folder layout

```
start.bat            double-click to run (finds Python + Kokoro, see tools/launch.py)
app/                 the app: web server (main.py), Kokoro narration (tts.py),
                     subtitles (captions.py), video rendering (render.py), interface (static/)
tools/               launcher and share-zip builder
sample/              a few test images
make_share_zip.bat   optional: build dist\StoryCraft.zip to hand over without GitHub

Created on your PC (not on GitHub):
.venv/               StoryCraft's Python environment (rebuilt automatically if needed)
config.json          where Kokoro and FFmpeg are on this PC
media/ projects/ exports/   your images, projects and videos
workspace/           caches and previews; safe to delete, or use *Clear video cache*
```
