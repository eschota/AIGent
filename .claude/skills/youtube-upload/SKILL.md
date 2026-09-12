---
name: youtube-upload
description: Upload a video to the owner's YouTube channel through their own Chrome with the chrome_* tools (YouTube Studio, shadow DOM, file input without a dialog), and where the DynIvy reel metadata lives.
tags: [aigent, youtube, chrome, upload, computer-use, dynivy, studio]
---

# Publishing a video on the owner's YouTube channel

The owner's Chrome is the tool: `mcp__aigent__chrome_open` relaunches it with a DevTools port on
their own profile (their Google login included), and the other `chrome_*` tools read and drive the
tab. YouTube Studio is Polymer: everything sits in shadow DOM, which `chrome_snapshot` walks; act
through refs from the latest snapshot, and take a new snapshot after every click that changes the
page.

## The flow that works

1. `chrome_open` with `https://studio.youtube.com/` and `restart_chrome: true` (Chrome usually runs
   without the port; it closes and comes back with its tabs). If the page is a Google sign-in, stop
   and ask the owner to sign in in that window, then `chrome_wait` for `url_contains: studio.youtube`.
2. Open the upload dialog: click the **Create** button (aria-label "Create"/"Создать", top right),
   then the menu item **Upload videos** ("Загрузить видео"). Alternative: `chrome_open`
   `https://studio.youtube.com/upload` directly.
3. `chrome_upload` with the absolute path of the file; the dialog owns an `input[type=file]`, so no
   OS dialog appears. Then `chrome_wait` for text "Details"/"Сведения" (up to 120 s).
4. Details step: `chrome_type` the title into the title box (role textbox, label "Add a title" or
   the current title), `clear: true`; the description into the description box. Below, choose the
   audience: **No, it's not made for kids** (radio). Click **Next** ("Далее") three times (Video
   elements, Checks, Visibility), waiting for each page with `chrome_snapshot`.
5. Visibility step: pick **Public**/**Unlisted**/**Private** as the owner said (the Asset Store
   media link needs Public). Copy the video link shown on that page (text `youtu.be/…` or
   `https://youtu.be/...`) from the snapshot before clicking **Publish**/**Save** ("Опубликовать").
6. `chrome_wait` for text "Video published"/"Видео опубликовано" (or the dialog with the link),
   `chrome_screenshot` for the record, then report the `watch?v=` URL. Processing may continue on
   YouTube's side; that does not block the link.

Refs go stale after any page change; a failed `chrome_click` means "snapshot again". Never type
the owner's password anywhere: a sign-in page is theirs to handle.

## DynIvy reel

Everything for the Dynamic Ivy reel is prepared in `R:\AssetStore\ASStore26\DynIvy\store-listing\YOUTUBE.md`:
the file (`R:\DynIvy-hq\reel\DynIvy_Reel_1080p.mp4`, 59 s, 1080p60), the exact title, the
description (each paragraph one line, paste as is — it carries the AI/ML disclosure that must not be
dropped), visibility **public**, and what to do with the URL afterwards (`youtube-receipt.json`,
`product.json` under `media.video.youtube`, `SUBMISSION.md`). Read that file first; do not invent a
title or description.
