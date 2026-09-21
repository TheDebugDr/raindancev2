"""Smoke-test the BrowserSession lifecycle by hand."""
from browser_session import BrowserSession


def main():
    with BrowserSession(profile_dir="my_profile", headless=False) as session:
        page = session.start(url="https://www.target.com/s?searchTerm=pokemon+cards").page
        session.wait()  # polite pause before touching the page

        print("Page title:", page.title())
        input("Press Enter to close browser...")
    # `with` guarantees close() — browser AND the Playwright driver — on exit.


if __name__ == "__main__":
    main()
