# YT Music Sorter — Design

Date: 2026-11-09
Status: approved 2026-11-09

## Intent

People with large YouTube Music "Liked Music" libraries want to listen to slices of it —
anime openings, K-pop, rock, or subtler cuts like "songs sung in Ukrainian" — but today
they must build those playlists by hand. This tool lets a user describe a playlist in plain
words, has Claude decide which liked songs fit, lets the user approve the result, and
creates the playlist on their YouTube Music account. Later, one button adds newly liked
songs that fit.

It is a free, open-source tool anyone can clone from GitHub and run on their own computer,
including non-technical users.

## Goals

- Create a YouTube Music playlist from a natural-language description, filled from the
  user's liked songs, decided by Claude.
- The user approves every playlist before it is created (checkbox list, see Flow 1).
- Update an existing tool-made playlist with matching songs liked since the last run,
  checking only songs never checked before for that playlist.
- Usable by non-technical users: one-click start, guided setup, clear progress feedback.
- Runs from a fresh clone with only: the Claude CLI installed and logged in, and a
  YouTube Music account.

## Non-goals

- Removing anything: songs are never removed from playlists (not even after the user
  unlikes them), and likes are never changed.
- Touching playlists the tool did not create — not editing, not reading them for changes.
- Hosted / multi-user service. It runs locally for one user per install.
- Automatic background or scheduled updates. Updates happen when the user presses a button.
- Other music services.

## Anti-goals (failure even if it "works")

- Any change to the user's likes or to playlists the tool did not create.
- A playlist created or songs added without the user seeing what happens.
- Sign-in data leaking: committed to git, logged, or sent anywhere except YouTube Music.
- A non-technical user getting stuck at a terminal error with no guidance.

## Constraints (decided by the user)

- **YouTube Music access:** the unofficial `ytmusicapi` Python library (not the official
  YouTube Data API). Liked songs come from its liked-songs call (the "Liked Music" list).
- **AI:** the Claude CLI (`claude`, non-interactive / print mode), using the user's own
  Claude login. No Anthropic API key.
- **Distribution:** clone (or download ZIP) from GitHub, authenticate, use. Assume the
  user already has the Claude CLI.
- **Interaction model:** "one-off requests" (mockup option A): describe → review → create.
  The app also keeps a list of the playlists it created, each with an update button.

## How it works

### Main screen

- A form: playlist description (free text) and playlist name, with a **Create** button.
- A list of playlists this tool created: name, description, song count, last updated,
  each with an **Update** button. An **Update all** button above the list.
- Setup status (Claude CLI found and logged in; YouTube Music connected). If either is
  missing, the app shows a guided setup screen instead of the main screen.

### Flow 1 — Create

1. User enters description + name, presses **Create**.
2. App fetches all liked songs.
3. App sends the songs to Claude in batches with the description; Claude returns, for each
   song, **include / exclude / unsure** and a short reason. Claude judges from the song's
   metadata (title, artists, album, duration) plus its own knowledge only — no web search,
   no lyrics lookups (decided by the user). "Unsure" is how Claude says it can't tell
   from that, instead of guessing.
4. **Review screen**, three sections, every song with a checkbox:
   - **Included:** songs Claude included, all checked by default. User can uncheck any.
   - **Unsure:** songs Claude couldn't decide, each with its reason. *(My call: unchecked
     by default; shown prominently so the user decides them.)*
   - **Not included:** excluded songs with Claude's reason, unchecked, collapsed by
     default, so the user can check one in. *(My call.)*
5. Nothing is written to YouTube Music until the user presses an approve button
   (e.g. "Create playlist with N songs"). User can also cancel, which discards the result.
6. On approve: create the playlist (private) on YouTube Music, add the checked songs,
   and save the playlist's record (below).

### Flow 2 — Update

1. User presses **Update** on a playlist (or **Update all**).
2. App fetches liked songs again.
3. App drops every song already in that playlist's record (included or excluded), leaving
   only songs never checked for this playlist.
4. If none are left, say so ("No new liked songs since last update") and stop.
5. Classify the new songs with Claude as in Flow 1, step 3.
6. Show the same review screen as Flow 1 for the new songs only (decided by the user).
   Nothing is added until the user approves; cancel discards the result and leaves the
   record unchanged, so those songs are checked again next time.
7. Add the approved new songs to the existing playlist and update the record (new songs go
   into included or excluded).

If the playlist no longer exists on YouTube Music (user deleted it there), Update reports
this clearly and does not recreate it silently. *(Builder decides the exact offer, e.g.
"remove from this list" or "recreate".)*

### Playlist record (the per-playlist "cache")

Saved to disk on the user's machine (survives restarts), one record per tool-made
playlist. The user suggested JSON; the format is the builder's call. Contents:

- YouTube Music playlist ID, name, description, created / last-updated times
- `included`: song IDs (YouTube video IDs) added to the playlist
- `excluded`: song IDs checked and not added (excluded, or unsure and left unchecked),
  each with Claude's reason
- For each song, whether its status came from Claude or from the user (unchecked / checked
  in on the review screen). User decisions are final: later runs never override them.

This record is also the tool's definition of "playlists it created": it only ever writes
to playlist IDs in these records.

Consequence the user accepted: if the user removes a song from a tool playlist by hand in
YouTube Music, Update will not re-add it (it was already checked).

### Loading and progress

Every long operation (fetching likes, classifying, creating, adding songs) shows a
progress bar and a plain-language line saying the current step, for example:

- "Fetching your liked songs… 1,200 of ~2,500"
- "Asking Claude which songs fit… batch 3 of 12"
- "Creating playlist on YouTube Music…"
- "Adding songs… 140 of 187"

Errors appear in plain language with what to do next (e.g. "YouTube Music sign-in
expired — reconnect", "Claude usage limit reached — try again later"). Work already done
should not be lost because one step fails. *(How much to retry/resume is the builder's
call.)* Only one operation runs at a time.

### Setup and sign-in

- **Start:** a `start.bat` (Windows) and `start.sh` (macOS / Linux) that install what is
  needed and open the app in the browser. My call: use `uv` so Python and dependencies are
  installed automatically.
- **Claude check:** on start, verify `claude` is on the PATH and logged in; if not, show a
  short guide.
- **YouTube Music sign-in:** an in-app step-by-step guide with screenshots for
  `ytmusicapi`'s browser-headers method (the user copies request headers from their
  browser's developer tools and pastes them into the app). This is the hardest step for
  non-technical users. The guide must be very clear, and the app must detect expired
  sign-in and send the user back to it.
- Sign-in data and playlist records live in a per-user app-data folder **outside the
  repo** (e.g. `%APPDATA%\yt-music-sorter` on Windows, the platform equivalent elsewhere),
  so downloading a new version (fresh ZIP or `git pull`) never loses them. *(My call.)*
  Nothing user-specific is ever written inside the repo.
- **README:** a non-technical guide covering download (including "Download ZIP" for people
  without git), first start, sign-in, and first playlist, plus a troubleshooting section.

## Calls made by me (Claude), not decided by the user

- Browser UI served from a local web server (localhost only), not a terminal UI, because
  non-technical users need it.
- Python backend (because `ytmusicapi` is Python); started with `uv` via start scripts.
- Playlist records persisted to disk rather than in memory.
- Button names: **Create**, **Update**, **Update all**.
- New playlists are **private**.
- UI language: English.
- Songs are sent to Claude in batches to keep large libraries within Claude usage limits.
- Editing a playlist's description after creation is **not in v1**. Under the add-only
  rule, a changed description could not remove songs that no longer fit. To change one,
  create a new playlist.

### Library size and Claude usage

The user's library is 1,000+ liked songs (exact number unknown). Target: work smoothly
for at least ~5,000 liked songs. *(My call.)* Claude's results are saved to disk batch by
batch while classifying. If a run stops partway (Claude usage limit, network error, app
closed), the user can continue it later without redoing finished batches. The review
screen opens once all batches are done. *(Builder decides the exact mechanics.)*

## Left to the builder

- Web framework, frontend approach (prefer no frontend build step, to keep cloning simple),
  and how progress reaches the browser (polling, SSE, etc.).
- Claude CLI invocation details: prompt wording, batch size, structured-output method,
  parsing and retry of malformed output, model choice.
- Record storage format and file layout.
- Handling of liked items that aren't songs (e.g. music videos) — treat them as songs
  unless that causes problems.
- Rate limiting / pacing of ytmusicapi calls.
- Visual design, within "intuitive for a non-technical user".
- Testing approach (YouTube Music and Claude should be mockable for tests).
