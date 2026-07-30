import asyncio
import json
import httpx
import re
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

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}

def save_config(data: dict) -> None:
    current = load_config()
    current.update(data)
    CONFIG_FILE.write_text(json.dumps(current, indent=4))

# Maps German user-config inputs to Goodreads English terminology
GOODREADS_LANG_MAP = {
    "deutsch": "german", "ger": "german", "de": "german",
    "englisch": "english", "eng": "english", "en": "english",
    "spanisch": "spanish", "spa": "spanish", "es": "spanish",
    "französisch": "french", "fra": "french", "fr": "french",
    "italienisch": "italian", "ita": "italian", "it": "italian",
    "japanisch": "japanese", "jap": "japanese", "jp": "japanese"
}

# --- STEP 1: GOODREADS AUTOCOMPLETE API ---
async def search_goodreads_async(query: str) -> list[dict]:
    url = "https://www.goodreads.com/book/auto_complete"
    params = {"format": "json", "q": query}
    
    async with httpx.AsyncClient() as client:
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            response = await client.get(url, params=params, headers=headers, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            
            books = []
            for item in data[:3]:
                work_id = item.get("workId")
                if not work_id:
                    continue
                    
                title = item.get("titleBare") or item.get("title") or "Unknown Title"
                author_data = item.get("author", {})
                author = author_data.get("name") if isinstance(author_data, dict) else "Unknown Author"
                
                books.append({
                    "title": title,
                    "author": author,
                    "work_id": str(work_id)
                })
            return books
        except Exception:
            return []


# --- STEP 1.5: GOODREADS MULTILINGUAL EDITIONS ---
async def fetch_goodreads_editions_async(work_id: str, preferred_languages: list[str]) -> list[str]:
    target_langs = [GOODREADS_LANG_MAP.get(lang.lower(), lang.lower()) for lang in preferred_languages]
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = await context.new_page()
        
        try:
            await page.goto(f"https://www.goodreads.com/work/editions/{work_id}?per_page=100&utf8=%E2%9C%93", timeout=15000)
            
            editions_data = await page.evaluate('''() => {
                const results = [];
                document.querySelectorAll('.dataRow .bookTitle').forEach(titleEl => {
                    const container = titleEl.closest('.elementList') || titleEl.parentElement.parentElement;
                    if (!container) return;

                    const langLabel = Array.from(container.querySelectorAll('.dataTitle')).find(el => el.textContent.includes('Edition language'));
                    
                    if (langLabel && langLabel.nextElementSibling) {
                        const lang = langLabel.nextElementSibling.textContent.trim().toLowerCase();
                        let title = titleEl.textContent.trim();
                        title = title.replace(/\\s*\\(.*?\\)\\s*$/, '');
                        results.push({title: title, language: lang});
                    }
                });
                return results;
            }''')
            
            localized_titles = []
            for item in editions_data:
                if item['language'] in target_langs and item['title'] not in localized_titles:
                    localized_titles.append(item['title'])
                    
            return localized_titles
            
        except PlaywrightTimeoutError:
            pass
        finally:
            await browser.close()
            
    return []


# --- STEP 2: VÖBB SCRAPER ---
async def scrape_voebb_async(query: str, preferred_languages: list[str]) -> list[dict]:
    if not preferred_languages:
        preferred_languages = ["deutsch", "englisch", "ger", "eng"]
        
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = await context.new_page()
        
        try:
            await page.goto("https://www.voebb.de/", timeout=20000)
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
                    if link: urls_to_check.append(link)

            found_branches = {} 
            for i, url in enumerate(urls_to_check):
                try:
                    if not (is_direct_hit and i == 0):
                        await page.goto(url, timeout=15000)
                        
                    await page.wait_for_selector('.aDISListe', timeout=8000)
                    
                    media_loc = page.locator('th:has-text("Medienart") + td')
                    if await media_loc.count() > 0:
                        media_html = (await media_loc.first.inner_html()).lower()
                        if "e-book" in media_html or "e-audio" in media_html or "online" in media_html:
                            continue
                            
                    lang_loc = page.locator('th:has-text("Sprache") + td')
                    language = (await lang_loc.first.inner_text()).lower() if await lang_loc.count() > 0 else ""
                    if language and not any(l in language for l in preferred_languages):
                        continue
                    
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
                                    if extra_info.startswith("-"): extra_info = extra_info[1:].strip()
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
                                found_branches[branch_name] = {"status": core_status, "info": extra_info}
                except (PlaywrightTimeoutError, Exception):
                    continue
                    
            return [{"branch": n, "status": d["status"], "info": d["info"]} for n, d in found_branches.items()]
            
        except (PlaywrightTimeoutError, Exception):
            return []
        finally:
            await browser.close()


# --- MODAL SCREENS ---
class SetupScreen(ModalScreen):
    def compose(self) -> ComposeResult:
        yield Container(
            Static("[b]Welcome to VÖBBRadar![/b]\n\nPlease configure your preferences.", id="setup-title"),
            Static("📍 Local Library Branches (comma-separated):", classes="setup-label"),
            Input(placeholder="e.g., Tiergarten, ZLB", id="branches-input"),
            Static("🗣️ Preferred Book Languages (comma-separated):", classes="setup-label"),
            Input(placeholder="e.g., deutsch, englisch, spanisch", id="languages-input"),
            Button("Save Config & Start", id="save-config-btn", variant="success"),
            id="setup-container"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-config-btn": self.action_save_config()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_save_config()

    def action_save_config(self) -> None:
        val_branches = self.query_one("#branches-input", Input).value
        val_langs = self.query_one("#languages-input", Input).value
        branches = [b.strip() for b in val_branches.split(",") if b.strip()]
        languages = [l.strip().lower() for l in val_langs.split(",") if l.strip()]
        
        save_config({"local_branches": branches, "preferred_languages": languages, "to_read_list": []})
        
        self.app.local_branches = branches
        self.app.preferred_languages = languages
        self.app.to_read_list = []
        self.app.pop_screen()

class ToReadScreen(ModalScreen):
    def compose(self) -> ComposeResult:
        yield Container(
            Static("[b]📋 Your To-Read List[/b]", id="toread-title"),
            OptionList(id="toread-list"),
            Input(placeholder="e.g. Die Außerirdischen Sayaka Murata", id="toread-input"),
            Horizontal(
                Button("Add", id="toread-add-btn", variant="success", classes="toread-btn"),
                Button("Remove Selected", id="toread-remove-btn", variant="error", classes="toread-btn"),
                Button("Close", id="toread-close-btn", variant="primary", classes="toread-btn"),
                classes="toread-buttons"
            ),
            id="toread-container"
        )

    def on_mount(self) -> None:
        lst = self.query_one("#toread-list", OptionList)
        for i, book in enumerate(self.app.to_read_list):
            lst.add_option(Option(book, id=f"tr_{i}"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        lst = self.query_one("#toread-list", OptionList)
        if event.button.id == "toread-close-btn":
            self.app.pop_screen()
            
        elif event.button.id == "toread-add-btn":
            inp = self.query_one("#toread-input", Input)
            val = inp.value.strip()
            if val and val not in self.app.to_read_list:
                self.app.to_read_list.append(val)
                save_config({"to_read_list": self.app.to_read_list})
                lst.add_option(Option(val, id=f"tr_{len(self.app.to_read_list)-1}"))
                inp.value = ""
                
        elif event.button.id == "toread-remove-btn":
            if lst.highlighted is not None:
                option = lst.get_option_at_index(lst.highlighted)
                if option.prompt in self.app.to_read_list:
                    self.app.to_read_list.remove(str(option.prompt))
                    save_config({"to_read_list": self.app.to_read_list})
                lst.clear_options()
                for i, book in enumerate(self.app.to_read_list):
                    lst.add_option(Option(book, id=f"tr_{i}"))


# --- TEXTUAL TUI APPLICATION ---
class VoebbSearchApp(App):
    CSS_PATH = "app.tcss"
    
    BINDINGS = [
        ("q", "quit", "Quit App"),
        ("t", "add_to_read", "Add to To-Read"),
        ("s", "scan_toread", "Scan To-Read List")
    ]
    
    def __init__(self):
        super().__init__()
        self.book_data_map = {}
        self.local_branches = []
        self.preferred_languages = []
        self.to_read_list = []
        
        self.spinner_timer = None
        self.spinner_index = 0
        self.spinner_frames = ["|", "/", "-", "\\"] 
        self.spinner_text = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="search-container"):
            with Horizontal(classes="search-bar"):
                yield Input(placeholder="Enter book title or author...", id="query-input")
                yield Button("Find Book", id="search-btn", variant="primary")
                yield Button("📋 To-Read List", id="manage-toread-btn")
            
            yield Static("Ready to search. (Press 't' to add a result, 's' to scan your To-Read list)", id="status-area")
            yield OptionList(id="book-selection-list")
            yield DataTable(id="results-table")
        yield Footer()

    def on_mount(self) -> None:
        config = load_config()
        if not config or not config.get("local_branches"):
            self.push_screen(SetupScreen())
        else:
            self.local_branches = config.get("local_branches", [])
            self.preferred_languages = config.get("preferred_languages", [])
            self.to_read_list = config.get("to_read_list", [])
            
            if self.to_read_list:
                self.run_worker(self.auto_scan_to_read_list(), name="auto_scanner")

        table = self.query_one("#results-table", DataTable)
        table.add_column("Status", key="status")
        table.add_column("Library Branch", key="branch")
        table.add_column("Info", key="info")
        table.add_column("Proximity", key="proximity")
        table.cursor_type = "row"

    # --- ACTION HANDLERS ---
    async def action_add_to_read(self) -> None:
        option_list = self.query_one("#book-selection-list", OptionList)
        
        if option_list.display and option_list.highlighted is not None:
            selected_id = option_list.get_option_at_index(option_list.highlighted).id
            book_data = self.book_data_map.get(selected_id)
            
            if book_data:
                final_query = book_data["query"]
                if final_query not in self.to_read_list:
                    self.to_read_list.append(final_query)
                    save_config({"to_read_list": self.to_read_list})
                    self.notify(f"Added: {final_query}", title="To-Read List", severity="information")
                else:
                    self.notify(f"Already in list: {final_query}", severity="warning")

    async def action_scan_toread(self) -> None:
        if not self.to_read_list:
            self.notify("Your To-Read list is currently empty!", title="To-Read Scan", severity="warning")
            return

        self.run_worker(self.manual_scan_to_read_list(), name="manual_scanner", exclusive=True)

    # --- SCANNER WORKERS ---
    async def manual_scan_to_read_list(self) -> None:
        """Scans all books in the background and populates the DataTable with hits!"""
        found_count = 0
        total_books = len(self.to_read_list)
        
        # Prepare the UI
        self.query_one("#book-selection-list", OptionList).display = False
        table = self.query_one("#results-table", DataTable)
        table.clear()
        table.display = True
        
        for i, book_query in enumerate(self.to_read_list):
            self.start_spinner(f"Scanning ({i + 1}/{total_books}): [b]{book_query}[/b]...")
            
            available_branches = await scrape_voebb_async(book_query, self.preferred_languages)
            
            local_hits = []
            for item in available_branches:
                if any(local.lower() in item["branch"].lower() for local in self.local_branches):
                    local_hits.append(item["branch"])
                    
                    # Dynamically add the hit to the table. 
                    # We inject the Book Title directly into the 'Info' column!
                    info_text = f"📖 {book_query}"
                    if item["info"]:
                        info_text += f" | {item['info']}"
                        
                    table.add_row(f"✅ {item['status']}", item["branch"], info_text, "< 20 mins")
            
            if local_hits:
                found_count += 1
                self.notify(
                    f"Available locally at: {', '.join(local_hits)}!", 
                    title=f"🎉 Hit: {book_query}", 
                    severity="information", 
                    timeout=15.0
                )
            
            await asyncio.sleep(2.0)

        self.stop_spinner()
        if found_count == 0:
            self.notify("None of your To-Read books are currently available at your preferred branches.", title="Scan Complete", severity="information")
            self.query_one("#status-area", Static).update("Scan complete! No local books found today.")
            table.display = False # Hide the empty table
        else:
            self.query_one("#status-area", Static).update(f"🎉 Scan complete! Found {found_count} book(s) available locally (listed below).")

    async def auto_scan_to_read_list(self) -> None:
        """Silent scanner that runs automatically on app launch with rate limiting."""
        for book_query in self.to_read_list:
            available_branches = await scrape_voebb_async(book_query, self.preferred_languages)
            
            local_hits = []
            for item in available_branches:
                if any(local.lower() in item["branch"].lower() for local in self.local_branches):
                    local_hits.append(item["branch"])
            
            if local_hits:
                self.notify(
                    f"Available locally at: {', '.join(local_hits)}!", 
                    title=f"📖 {book_query}", 
                    severity="information", 
                    timeout=15.0
                )
            await asyncio.sleep(2.0)

    # --- SPINNER LOGIC ---
    def update_spinner(self) -> None:
        self.spinner_index = (self.spinner_index + 1) % len(self.spinner_frames)
        frame = self.spinner_frames[self.spinner_index]
        self.query_one("#status-area", Static).update(f"{frame} {self.spinner_text}")

    def start_spinner(self, text: str) -> None:
        self.spinner_text = text
        self.spinner_index = 0
        if self.spinner_timer is not None: self.spinner_timer.stop()
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
        elif event.button.id == "manage-toread-btn":
            self.push_screen(ToReadScreen())

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
        self.start_spinner("Fetching metadata from Goodreads...")
        
        option_list = self.query_one("#book-selection-list", OptionList)
        option_list.clear_options()
        self.book_data_map.clear()
        
        books = await search_goodreads_async(query)
        self.stop_spinner() 
        
        if not books:
            self.query_one("#status-area", Static).update("❌ No results found. Try Direct Search below.")
        else:
            self.start_spinner("Discovering localized editions via Goodreads...")
            
            async def get_variants(book):
                titles = [book['title']]
                if book.get('work_id'):
                    localized = await fetch_goodreads_editions_async(book['work_id'], self.preferred_languages)
                    for t in localized:
                        if t not in titles:
                            titles.append(t)
                return book['author'], titles

            tasks = [get_variants(b) for b in books[:2]]
            results = await asyncio.gather(*tasks)
            self.stop_spinner()

            option_index = 0
            for author, titles in results:
                for t in titles:
                    label = f"📖 {t} by {author}"
                    option_id = f"book_{option_index}"
                    option_list.add_option(Option(label, id=option_id))
                    self.book_data_map[option_id] = {
                        "query": f"{t} {author}",
                        "title": t,
                        "author": author
                    }
                    option_index += 1
        
        bypass_id = "bypass_query"
        option_list.add_option(Option(f"🚀 Direct VÖBB Search for: \"{query}\"", id=bypass_id))
        self.book_data_map[bypass_id] = {"query": query, "title": query, "author": ""}
        
        option_list.display = True
        self.query_one("#status-area", Static).update("✨ Select a title/edition below. (Press 't' to add to To-Read)")
        self.query_one("#search-btn", Button).disabled = False

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "book-selection-list":
            selected_id = event.option.id
            voebb_query = self.book_data_map[selected_id]["query"]
            
            self.query_one("#book-selection-list").display = False
            self.query_one("#results-table").display = True
            self.query_one("#results-table", DataTable).clear()
            
            self.start_spinner(f"Digging through VÖBB branches for: [b]{voebb_query}[/b]...")
            self.run_worker(scrape_voebb_async(voebb_query, self.preferred_languages), exclusive=True, name="voebb_scraper")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "voebb_scraper":
            return
            
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            self.stop_spinner()

        if event.state == WorkerState.SUCCESS:
            self.display_results(event.worker.result)
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
            is_local = any(local.lower() in item["branch"].lower() for local in self.local_branches)
            if is_local:
                local_hits.append((item["branch"], item["status"], item["info"]))
            else:
                distant_hits.append((item["branch"], item["status"], item["info"]))

        for branch, status, info in local_hits:
            table.add_row(f"✅ {status}", branch, info, "< 20 mins")

        for branch, status, info in distant_hits:
            table.add_row(f"📍 {status}", branch, info, "> 20 mins")

        if local_hits:
            status_area.update(f"🎉 Found at [bold green]{len(local_hits)} local branch(es)[/bold green]! (Locals pinned to top)")
        else:
            status_area.update(f"⚠️ Not nearby. Found at {len(distant_hits)} distant branch(es).")

if __name__ == "__main__":
    app = VoebbSearchApp()
    app.run()