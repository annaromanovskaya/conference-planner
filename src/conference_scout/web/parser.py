from bs4 import BeautifulSoup


def extract_page_text(html: str, *, max_chars: int) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form"]):
        node.decompose()
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    text = "\n".join(line for line in lines if line)
    return text[:max_chars]
