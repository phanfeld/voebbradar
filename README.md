# 📡 VÖBBRadar

A lightning-fast, terminal-native application to search the Berlin Public Library (VÖBB) network for physical books. 

This tool bypasses VÖBB's clunky, stateful frontend and slow catalog APIs. It combines the speed of the OpenLibrary REST API for exact metadata matching with a robust, headless Playwright scraper to check live shelf availability across Berlin—directly from your terminal.

All vibe-coded in 2h.

## ✨ Features

- **Blazing Fast Metadata:** Uses OpenLibrary to resolve Title, Author, and Language in milliseconds before ever touching the library catalog.
- **Foolproof Scraping:** Bypasses legacy UI limitations by parsing the structured DOM, safely ignoring false positives like e-books, audiobooks, and incorrect languages.
- **Smart Proximity Sorting:** Pins your preferred local branches to the very top of the results list.
- **Condition Reporting:** Isolates specific shelf conditions (e.g., "Wasserschaden", "Bestellt") into a clean, separate column.
- **Sleek TUI:** Built with Textual for a modern, responsive, and mouse-friendly Command Line Interface.

### To be added
- **Time to reach library branch via public transport**: Include a column with the current approx. time to reach that library branch via public transport from your location without leaking private data. This should maybe replace the need for the user to specify their preferred libraries.
- **Include other media types**: Currently, the app only searches for physical copies of books. I also want to search for DVDs, BlueRays, Video Games, etc.
- **Consistent language**: Multi-language support and consistency throughout.

## 🛠️ Installation & Setup

Ensure you have Python 3 installed. If you are on a Debian/Ubuntu-based system, make sure the `venv` package is installed first:

```bash
sudo apt install python3-venv
```

**1. Clone or download the repository, then navigate into the folder:**
```bash
cd path/to/voebb-radar
```

**2. Create and activate a virtual environment:**
```bash
python3 -m venv venv
source venv/bin/activate
```

**3. Install the required Python packages:**
```bash
pip install textual playwright httpx
```

**4. Install the Playwright browser binaries and system dependencies:**
```bash
playwright install
playwright install-deps
```

## 🚀 Usage

Make sure your virtual environment is activated, then run the app:

```bash
python app.py
```

### First-Run Setup
The first time you launch the application, a modal will prompt you to enter your preferred local library branches (e.g., `Mark Twain`). Can handle partial library names. Beware that `Mitte` will maybe add libraries to your preferred libraries that are not easy to reach.

The app saves these to a local `voebb_config.json` file. Whenever a book you search for is physically available at one of these branches, it will be automatically highlighted and pinned to the top of your results list.

### How to use the TUI
1. Type a book title or author and press `Enter`.
2. Use the arrow keys or your mouse to select the exact edition/language from the OpenLibrary results.
3. Press `Enter` to unleash the scraper.
4. Press `q` or `Ctrl+C` to quit the application at any time.

## 📂 File Structure

- `app.py`: The main application logic (API requests, Playwright scraper, Textual UI).
- `app.tcss`: The stylesheet controlling the terminal interface's layout, animations, and colors.
- `voebb_config.json`: Auto-generated on first run, stores your preferred local branches.