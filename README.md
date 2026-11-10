# YT Music Sorter

Turn your YouTube Music liked songs into playlists by describing them.

Write what you want, for example *"Upbeat songs for running, no slow ballads"* or *"Instrumental music for studying"*. Claude goes through your liked songs and picks the ones that fit. You review the list, tick or untick songs, and only then is a private playlist created on your account. Later, **Update** adds the songs you liked since.

![The review screen: songs Claude picked, each with a checkbox](docs/images/review-screen.png)

## Requirements

- **Claude Code**, installed and logged in with your Claude account ([install guide](https://docs.claude.com/en/docs/claude-code/setup)). Check it by running `claude` in a terminal.
- **A YouTube Music account** with liked songs.
- **Windows, macOS or Linux** with an internet connection. Nothing else: the start script installs everything the app needs.

## Usage

### 1. Download and start

1. Download the app: click **Code → Download ZIP** on this page and unzip it, or run:
   ```
   git clone https://github.com/TymchukRoman/ytm-playlist-creator.git
   ```
2. Start it:
   - **Windows:** double-click `start.bat`.
   - **macOS / Linux:** run `./start.sh` in a terminal in the app folder.

The app opens in your browser. Keep the window that opened alongside it running while you use the app, and close it to stop the app. The first start takes a minute or two.

### 2. Connect YouTube Music (once)

The setup screen walks you through this:

1. Open [music.youtube.com](https://music.youtube.com) and sign in.
2. Press `F12` (on a Mac, `Cmd`+`Option`+`I`) and open the **Network** tab.
3. Type `browse` in the filter box. If nothing shows up, click **Home** on the YouTube Music page.
4. Right-click the first request:
   - **Chrome / Edge:** choose **Copy → Copy as cURL (bash)**.
   - **Firefox:** click the request, switch on **Raw** next to **Request Headers**, and copy that text.
5. Paste it into the app and press **Connect**.

What you copy acts like a password for your YouTube account. The app keeps it only on your computer.

### 3. Make a playlist

1. Describe which songs belong in it and give it a name.
2. Press **Create** and wait while Claude checks your songs. A progress bar shows how far along it is.
3. Review the result:
   - **Included** songs are ticked.
   - **Unsure** and **Not included** songs are unticked.
   - Change anything you like.
4. Press **Create playlist**. Nothing changes on YouTube Music before this.

![The main screen with a playlist and its Update button](docs/images/main-screen.png)

### 4. Keep it up to date

Press **Update** on a playlist, or **Update all**, to check songs you liked since last time. You review those the same way.

## Good to know

- **Nothing is removed.** The app only creates playlists and adds songs. It never removes songs, changes your likes, or touches playlists it didn't create.
- **Claude only sees song info:** title, artists, album and length. For songs it doesn't know, it answers "Unsure".
- **Model and effort:** under the playlist name, open **Claude: …** to choose the model and effort. A bigger model or more effort is more careful, but slower and uses more of your Claude limit.
- **It learns as you go.** Each scan tags your songs. A later playlist on the same simple topic (for example *"Jazz only"*) reuses what earlier scans settled and asks Claude only about the rest. Tick **Ask Claude about every song again** to skip that.
- **Pauses are safe.** If your Claude limit runs out or the YouTube sign-in expires, the run pauses and keeps its progress. Press **Resume** later.
- **Your data:** the sign-in and the app's records are stored in your user data folder, not in the app folder, so updating the app keeps them:
  - Windows: `%LOCALAPPDATA%\yt-music-sorter`
  - macOS: `~/Library/Application Support/yt-music-sorter`
  - Linux: `~/.local/share/yt-music-sorter`

  **Disconnect** at the bottom of the main screen removes your sign-in.

## Troubleshooting

- **"Claude isn't logged in":** run `claude` in a terminal and log in, then press **Check again**.
- **"Your Claude Code is too old":** run `claude update`.
- **Windows warns about `start.bat`:** click **More info → Run anyway**.
- **The browser didn't open:** copy the address printed in the app's window (like `http://127.0.0.1:8765/`) into your browser.

Something else? [Open an issue](https://github.com/TymchukRoman/ytm-playlist-creator/issues).

## For developers

Python (FastAPI) with a plain HTML/JS page and no build step, managed with [uv](https://docs.astral.sh/uv/).

```
uv run pytest                     # tests
uv run ytsorter                   # start the app
YTSORTER_DEMO=1 uv run ytsorter   # demo: sample songs and a pretend Claude, no accounts needed
```

`YTSORTER_DATA_DIR` sets a different data folder, and `YTSORTER_NO_BROWSER=1` stops the app from opening a browser. The screenshots above come from demo mode.
