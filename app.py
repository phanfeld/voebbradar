import asyncio
import json
import httpx
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Header, Footer, Input, Button, Static, OptionList, DataTable
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerState

# --- CONFIGURATION PATH ---
CONFIG_FILE = Path("voebb_config.json")


# --- STEP 1: OPENLIBRARY API ---
async def search_openlibrary_async(query: str) -> list[dict]:
    """Fetches clean book metadata instantly via REST API."""
    url = "https://openlibrary.org/search.json"
    params = {
        "q": query,
        "fields": "title,author_name,language",
        "limit": 5
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, params=params, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            
            books = []
            for doc in data.get("docs", []):
                title = doc.get("title", "Unknown Title")
                authors = doc.get("author_name", ["Unknown Author"])
                author = authors[0] if authors else "Unknown Author"
                languages = doc.get("language", [])
                
                books.append({
                    "title": title,
                    "author": author,
                    "languages": languages
                })
            return books
        except httpx.RequestError as e:
            return []


# --- STEP 2: VÖBB SCRAPER ---
async def scrape_voebb_async(query: str, preferred_languages: list[str]) -> list[dict]:
    """Scrapes VÖBB relying purely on table structure, isolating extra info."""
    
    # Fallback just in case config is empty
    if not preferred_languages:
        preferred_languages = ["deutsch", "englisch", "ger", "eng"]
        
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()
        
        try:
            await page.goto("https://www.voebb.de/", timeout=15000)
            await page.wait_for_selector('#Autosuggest', timeout=10000)
            await page.fill('#Autosuggest', query)
            await page.keyboard.press("Enter")
            
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
                await page.wait_for_selector('li.rList_li, .aDISListe', timeout=10000)
            except PlaywrightTimeoutError:
                return [] 
            
            urls_to_check = []
            is_direct_hit = False
            
            if await page.locator('.aDISListe').count() > 0:
                is_direct_hit = True
                urls_to_check.append(page.url)
            else:
                results = await page.locator('.rList_titel a').all()
                for result in results[:3]: 
                    link = await result.get_attribute('href')
                    if link:
                        urls_to_check.append(link)

            found_branches = {} 
            
            for i, url in enumerate(urls_to_check):
                if not (is_direct_hit and i == 0):
                    await page.goto(url)
                
                try:
                    await page.wait_for_selector('.aDISListe', timeout=8000)
                    
                    # 1. Media Type Check
                    media_loc = page.locator('th:has-text("Medienart") + td')
                    if await media_loc.count() > 0:
                        media_html = (await media_loc.first.inner_html()).lower()
                        if "e-book" in media_html or "e-audio" in media_html or "online" in media_html:
                            continue
                            
                    # 2. Dynamic Language Check
                    lang_loc = page.locator('th:has-text("Sprache") + td')
                    language = (await lang_loc.first.inner_text()).lower() if await lang_loc.count() > 0 else ""
                    if language and not any(l in language for l in preferred_languages):
                        continue
                    
                    # 3. Availability Check
                    await page.wait_for_selector('.register-table tr', timeout=5000)
                    rows = await page.locator('.register-table tr').all()
                    
                    for row in rows:
                        tds = await row.locator('td').all()
                        
                        if len(tds) >= 4:
                            branch_name = (await tds[0].inner_text()).strip()
                            status_text = (await tds[4].inner_text()).strip()
                            
                            span_el = tds[4].locator('span')
                            span_class = ""
                            core_status = status_text
                            extra_info = ""
                            
                            if await span_el.count() > 0:
                                span_class = (await span_el.first.get_attribute('class') or "").lower()
                                span_text = (await span_el.first.inner_text()).strip()
                                
                                if span_text:
                                    core_status = span_text
                                    extra_info = status_text.replace(span_text, "").strip()
                                    if extra_info.startswith("-"):
                                        extra_info = extra_info[1:].strip()
                            else:
                                if " - " in status_text:
                                    parts = status_text.split(" - ", 1)
                                    core_status = parts[0].strip()
                                    extra_info = parts[1].strip()
                            
                            is_available = False
                            
                            if "notavailable" in span_class:
                                is_available = False
                            elif "available" in span_class:
                                is_available = True
                            else:
                                negative_keywords = ["nicht ", "entliehen", "bestellt", "vermisst", "bearbeitung", "präsenz"]
                                is_available = not any(kw in status_text.lower() for kw in negative_keywords)
                            
                            if is_available:
                                found_branches[branch_name] = {
                                    "status": core_status, 
                                    "info": extra_info
                                }
                                
                except PlaywrightTimeoutError:
                    continue

            return [
                {
                    "branch": name, 
                    "status": data["status"], 
                    "info": data["info"]
                } for name, data in found_branches.items()
            ]
            
        finally:
            await browser.close()


# --- SETUP MODAL SCREEN ---
class SetupScreen(ModalScreen):
    """Screen to ask the user for their preferred branches and languages on first run."""
    
    def compose(self) -> ComposeResult:
        yield Container(
            Static("[b]Welcome to VÖBBRadar![/b]\n\nPlease configure your preferences.", id="setup-title"),
            
            Static("📍 Local Library Branches (comma-separated):", classes="setup-label"),
            Input(placeholder="e.g., Ingeborg-Bachmann, Tiergarten, Mark Twain, ...", id="branches-input"),
            
            Static("🗣️ Preferred Book Languages (comma-separated):", classes="setup-label"),
            Input(placeholder="e.g., deutsch, englisch, spanisch", id="languages-input"),
            
            Button("Save Config & Start", id="save-config-btn", variant="success"),
            id="setup-container"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-config-btn":
            self.action_save_config()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_save_config()

    def action_save_config(self) -> None:
        val_branches = self.query_one("#branches-input", Input).value
        val_langs = self.query_one("#languages-input", Input).value
        
        # Split by comma, clean up whitespace, and lower-case the languages
        branches = [b.strip() for b in val_branches.split(",") if b.strip()]
        languages = [l.strip().lower() for l in val_langs.split(",") if l.strip()]
        
        # Save to JSON
        config_data = {
            "local_branches": branches,
            "preferred_languages": languages
        }
        
        try:
            CONFIG_FILE.write_text(json.dumps(config_data, indent=4))
        except IOError as e:
            self.app.notify(f"Error saving config: {e}", severity="error")
            
        # Update the app state and dismiss the modal
        self.app.local_branches = branches
        self.app.preferred_languages = languages
        self.app.pop_screen()


# --- TEXTUAL TUI APPLICATION ---
class VoebbSearchApp(App):
    """A sleek two-step terminal interface for checking VÖBB physical inventory."""
    
    CSS_PATH = "app.tcss"
    BINDINGS = [("q", "quit", "Quit App")]
    
    def __init__(self):
        super().__init__()
        self.book_data_map = {}
        self.local_branches = []
        self.preferred_languages = []
        
        self.spinner_timer = None
        self.spinner_index = 0
        self.spinner_frames = ["|", "/", "-", "\\"] 
        self.spinner_text = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        
        with Container(id="search-container"):
            with Horizontal():
                yield Input(placeholder="Enter book title or author...", id="query-input")
                yield Button("Find Book", id="search-btn", variant="primary")
            
            yield Static("Ready to search.", id="status-area")
            
            yield OptionList(id="book-selection-list")
            yield DataTable(id="results-table")
            
        yield Footer()

    def on_mount(self) -> None:
        # Check for config file on startup
        if CONFIG_FILE.exists():
            try:
                config_data = json.loads(CONFIG_FILE.read_text())
                self.local_branches = config_data.get("local_branches", [])
                self.preferred_languages = config_data.get("preferred_languages", [])
            except json.JSONDecodeError:
                self.push_screen(SetupScreen())
        else:
            self.push_screen(SetupScreen())

        table = self.query_one("#results-table", DataTable)
        table.add_column("Status", key="status")
        table.add_column("Library Branch", key="branch")
        table.add_column("Info", key="info")
        # table.add_column("Proximity", key="proximity")
        table.cursor_type = "row"

    # --- SPINNER LOGIC ---
    def update_spinner(self) -> None:
        self.spinner_index = (self.spinner_index + 1) % len(self.spinner_frames)
        frame = self.spinner_frames[self.spinner_index]
        self.query_one("#status-area", Static).update(f"{frame} {self.spinner_text}")

    def start_spinner(self, text: str) -> None:
        self.spinner_text = text
        self.spinner_index = 0
        if self.spinner_timer is not None:
            self.spinner_timer.stop()
        self.spinner_timer = self.set_interval(0.1, self.update_spinner)

    def stop_spinner(self) -> None:
        if self.spinner_timer is not None:
            self.spinner_timer.stop()
            self.spinner_timer = None
            
        self.query_one("#status-area", Static).update("")

    # --- UI EVENT HANDLERS ---
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "search-btn":
            await self.action_submit_search()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "query-input":
            await self.action_submit_search()

    async def action_submit_search(self) -> None:
        query = self.query_one("#query-input", Input).value.strip()
        if not query:
            self.query_one("#status-area", Static).update("⚠️ Please enter a search query.")
            return

        self.query_one("#search-btn", Button).disabled = True
        self.query_one("#results-table").display = False
        
        self.start_spinner("Fetching catalog data from OpenLibrary...")
        
        option_list = self.query_one("#book-selection-list", OptionList)
        option_list.clear_options()
        self.book_data_map.clear()
        
        books = await search_openlibrary_async(query)
        
        self.stop_spinner() 
        
        if not books:
            self.query_one("#status-area", Static).update("❌ No results found on OpenLibrary.")
            self.query_one("#search-btn", Button).disabled = False
            return
            
        for index, book in enumerate(books):
            lang_str = ", ".join(book["languages"][:3]) if book["languages"] else "unknown"
            label = f"📖 {book['title']} by {book['author']} [{lang_str}]"
            option_id = f"book_{index}"
            
            option_list.add_option(Option(label, id=option_id))
            self.book_data_map[option_id] = f"{book['title']} {book['author']}"
            
        option_list.display = True
        self.query_one("#status-area", Static).update("✨ Select the correct book from the list below.")
        self.query_one("#search-btn", Button).disabled = False

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        selected_id = event.option.id
        voebb_query = self.book_data_map[selected_id]
        
        self.query_one("#book-selection-list").display = False
        self.query_one("#results-table").display = True
        
        table = self.query_one("#results-table", DataTable)
        table.clear()
        
        self.start_spinner(f"Digging through VÖBB branches for: [b]{voebb_query}[/b]...")
        
        # Pass the preferred languages from state to the background scraper
        self.run_worker(scrape_voebb_async(voebb_query, self.preferred_languages), exclusive=True, name="voebb_scraper")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "voebb_scraper":
            return
            
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            self.stop_spinner()

        if event.state == WorkerState.SUCCESS:
            available_branches = event.worker.result
            self.display_results(available_branches)
        elif event.state == WorkerState.ERROR:
            self.query_one("#status-area", Static).update(f"❌ Scraper Error: {event.worker.error}")

    def display_results(self, available_branches: list[dict]) -> None:
        table = self.query_one("#results-table", DataTable)
        status_area = self.query_one("#status-area", Static)

        if not available_branches:
            status_area.update("❌ Found the book, but currently NOT physically available at any branch.")
            return

        local_hits = []
        distant_hits = []

        for item in available_branches:
            branch_name = item["branch"]
            status_text = item["status"]
            info_text = item["info"]
            
            is_local = any(local.lower() in branch_name.lower() for local in self.local_branches)
            if is_local:
                local_hits.append((branch_name, status_text, info_text))
            else:
                distant_hits.append((branch_name, status_text, info_text))

        for branch, status, info in local_hits:
            table.add_row(f"✅ {status}", branch, info)

        for branch, status, info in distant_hits:
            table.add_row(f"📍 {status}", branch, info)

        if local_hits:
            status_area.update(f"🎉 Found at [bold green]{len(local_hits)} local branch(es)[/bold green]! (Locals pinned to top)")
        else:
            status_area.update(f"⚠️ Not nearby. Found at {len(distant_hits)} distant branch(es).")

if __name__ == "__main__":
    app = VoebbSearchApp()
    app.run()