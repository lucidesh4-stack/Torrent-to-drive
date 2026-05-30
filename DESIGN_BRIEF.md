# Streamly — UI/UX Design Brief

## 1. What Streamly Is
Streamly is a private, lightweight web app built to act as a seamless frontend for **Seedr.cc** (a cloud torrenting service). 

The goal is to allow a small group of non-technical friends to securely search for movies/shows, add them to a shared cloud drive, and stream them directly in the browser. It bypasses the need for them to install torrent software, set up accounts, or configure VPNs.

**Technical Constraints:**
*   **No heavy frameworks:** It uses pure vanilla HTML, CSS, and JavaScript.
*   **API Quotas:** Torrent searches hit a third-party API with strict limits (200 requests/day). Therefore, we cannot do "search-as-you-type" for torrents; the user must explicitly click a "Search" button.
*   **Single-Account Architecture:** The backend is hardcoded to a single Seedr account. Visitors do not log in. They just open the link and start interacting with the shared drive immediately.

---

## 2. The Complete User Workflow

There are two main screens: the **Search Tab** and the **Cloud Drive Tab**. 

### Flow A: Finding and Adding Content (Search Tab)
1.  **Intent:** The user wants to watch a movie. They land on the "Search" tab.
2.  **Input:** They begin typing "Avengers" into the unified search bar.
3.  **IMDb Assist:** As they type, a dropdown appears suggesting actual IMDb titles. They click the correct suggestion to auto-fill the search box.
4.  **Search Execution:** The user clicks the **[Search]** button. A table of torrent results appears (showing Seeds, Leeches, Size, and Date).
5.  **Adding:** The user clicks the **[Add]** button next to a high-seed torrent. 
6.  **Backend Magic:** The app sends that magnet link to Seedr and logs it to a global History database. A small toast notification pops up saying "✓ Added".

### Flow B: The "Power User" Magnet Paste (Search Tab)
1.  **Intent:** A user already has a magnet link from a different website.
2.  **Input:** They click the **[📋 Paste]** button (which pulls from their clipboard) or manually paste `magnet:?xt=urn:btih:...` into the main search bar.
3.  **Dynamic UI Shift:** The UI detects the magnet link format. The blue **[Search]** button instantly morphs into a **[Add Link]** button.
4.  **Execution:** The user clicks **[Add Link]** (or presses Enter). The magnet is sent directly to Seedr, bypassing the search API entirely.

### Flow C: Watching Content (Cloud Drive Tab)
1.  **Intent:** The user wants to watch what they just downloaded. They switch to the "Cloud Drive" tab.
2.  **Browsing:** A table displays files and folders currently sitting in the cloud drive.
3.  **Selection:** The user clicks a video file. The right-hand "Side Card" updates to show the file's size and enables action buttons.
4.  **Playback:** The user clicks **[Open]**. A sleek, dark video overlay pops up on the screen, streaming the video directly from Seedr.

### Flow D: Managing the Drive (Cloud Drive Tab / History)
1.  **Cleanup:** The user selects multiple watched files using checkboxes. The right-hand Side Card totals up their size. They click **[Delete]**.
2.  **Zipping:** If they select a folder and click **[Download]**, the backend automatically zips the folder and provides a direct download link.
3.  **History:** If a download failed or they lost a link, they can click the **[🕒 History]** button in the top right. A modal opens showing the last 50 magnets added across all devices, allowing them to re-add them or open them instantly in a third-party ephemeral streamer (Webtor.io).

---

## 3. What We Need Help With

The app is functionally perfect, but the UI is a bit rigid and "developer-designed." We are looking for advice on:

1.  **The Unified Search Bar:** We recently merged the "Paste", "Search Input", "Category Dropdown", and "Action Buttons" into a single horizontal row. Does it feel too cluttered? How can we make the transition between "Search Mode" and "Magnet Mode" feel natural?
2.  **Data Tables:** The Cloud Drive and Search results are dense tables. How can we make them look more modern, scannable, and "Netflix-like," while still keeping vital data like Seeders/Leechers visible?
3.  **The Split Layout (Cloud Drive):** The Cloud Drive uses a master/detail view (table on the left, "Selected Item" side-card on the right). Does this layout feel balanced? Are the action buttons (Open, Download, Delete) easily discoverable?
4.  **Mobile Experience:** The dense tables currently collapse into "cards" on mobile. How can we improve the mobile ergonomics of finding, selecting, and deleting files?
