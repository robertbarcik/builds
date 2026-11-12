"""HTML processing pipeline — extract what an AI agent sees from raw HTML."""

import json
import re
from bs4 import BeautifulSoup, Comment


def process_html(html_content: str) -> dict:
    """Process HTML and return agent-parsed view with stats."""
    soup = BeautifulSoup(html_content, "lxml")

    agent_view = {
        "text_content": _extract_visible_text(soup),
        "headings": _extract_headings(soup),
        "links": _extract_links(soup),
        "images": _extract_images(soup),
        "structured_data": _extract_structured_data(soup),
        "forms": _extract_forms(soup),
        "meta": _extract_meta(soup),
        "js_dependent_elements": _find_js_dependent(soup),
        "semantic_elements": _extract_semantic_elements(soup),
    }

    stats = _compute_stats(html_content, soup, agent_view)

    return {"agent_view": agent_view, "stats": stats}


def _extract_visible_text(soup: BeautifulSoup) -> str:
    """Get text that a simple scraper would see (no JS execution)."""
    clone = BeautifulSoup(str(soup), "lxml")

    # Remove script, style, noscript, template tags
    for tag in clone.find_all(["script", "style", "noscript", "template"]):
        tag.decompose()

    # Remove HTML comments
    for comment in clone.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    text = clone.get_text(separator="\n", strip=True)
    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _extract_headings(soup: BeautifulSoup) -> list:
    """Extract heading hierarchy."""
    headings = []
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        text = tag.get_text(strip=True)
        if text:
            headings.append({
                "level": int(tag.name[1]),
                "text": text,
            })
    return headings


def _extract_links(soup: BeautifulSoup) -> list:
    """Extract all links with text and href."""
    links = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        links.append({
            "text": text or "[no text]",
            "href": a["href"],
        })
    return links


def _extract_images(soup: BeautifulSoup) -> list:
    """Extract images with alt text info."""
    images = []
    for img in soup.find_all("img"):
        images.append({
            "src": img.get("src", ""),
            "alt": img.get("alt", ""),
            "has_alt": bool(img.get("alt", "").strip()),
        })
    return images


def _extract_structured_data(soup: BeautifulSoup) -> list:
    """Extract JSON-LD and other structured data."""
    structured = []

    # JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            structured.append({"type": "JSON-LD", "data": data})
        except (json.JSONDecodeError, TypeError):
            pass

    # Microdata (just detect presence)
    microdata_elements = soup.find_all(attrs={"itemscope": True})
    for el in microdata_elements[:5]:  # limit to 5
        item_type = el.get("itemtype", "")
        structured.append({
            "type": "Microdata",
            "data": {"itemtype": item_type, "tag": el.name},
        })

    return structured


def _extract_forms(soup: BeautifulSoup) -> list:
    """Extract form information."""
    forms = []
    for form in soup.find_all("form"):
        fields = []
        for inp in form.find_all(["input", "select", "textarea"]):
            fields.append({
                "type": inp.get("type", inp.name),
                "name": inp.get("name", ""),
                "placeholder": inp.get("placeholder", ""),
            })
        forms.append({
            "action": form.get("action", ""),
            "method": form.get("method", "GET"),
            "fields": fields,
        })
    return forms


def _extract_meta(soup: BeautifulSoup) -> dict:
    """Extract meta tags and Open Graph data."""
    meta = {}

    # Title
    title_tag = soup.find("title")
    if title_tag:
        meta["title"] = title_tag.get_text(strip=True)

    # Meta description
    desc = soup.find("meta", attrs={"name": "description"})
    if desc and desc.get("content"):
        meta["description"] = desc["content"]

    # Open Graph
    for og in soup.find_all("meta", attrs={"property": re.compile(r"^og:")}):
        if og.get("content"):
            meta[og["property"]] = og["content"]

    # Canonical
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        meta["canonical"] = canonical["href"]

    # Robots
    robots = soup.find("meta", attrs={"name": "robots"})
    if robots and robots.get("content"):
        meta["robots"] = robots["content"]

    return meta


def _find_js_dependent(soup: BeautifulSoup) -> list:
    """Detect elements that likely need JavaScript to render content."""
    js_dependent = []

    # Common SPA root elements
    for selector in ["#app", "#root", "[data-react-root]", "[ng-app]", "[data-v-app]"]:
        for el in soup.select(selector):
            text = el.get_text(strip=True)
            if not text or text in ("Loading...", "Loading", "Please wait...", ""):
                js_dependent.append({
                    "element": f"<{el.name} id='{el.get('id', '')}'>",
                    "content": text or "[empty]",
                    "reason": "SPA root element with no server-rendered content",
                })

    # Elements with loading placeholders
    loading_patterns = ["loading...", "please wait", "loading content", "initializing"]
    for el in soup.find_all(string=re.compile("|".join(loading_patterns), re.I)):
        parent = el.parent
        if parent and parent.name not in ("script", "noscript", "style"):
            js_dependent.append({
                "element": f"<{parent.name} class='{' '.join(parent.get('class', []))}'>",
                "content": str(el).strip()[:100],
                "reason": "Loading placeholder text found",
            })

    # Elements with framework directives
    framework_attrs = ["v-if", "v-for", "ng-if", "ng-repeat", "x-show", "x-for", ":class"]
    for attr in framework_attrs:
        for el in soup.find_all(attrs={attr: True}):
            js_dependent.append({
                "element": f"<{el.name} {attr}='{el[attr]}'>",
                "content": el.get_text(strip=True)[:100] or "[empty]",
                "reason": f"Framework directive: {attr}",
            })

    # Template tags
    for tmpl in soup.find_all("template"):
        js_dependent.append({
            "element": "<template>",
            "content": tmpl.get_text(strip=True)[:100] or "[empty]",
            "reason": "Template element (not rendered without JS)",
        })

    return js_dependent


def _extract_semantic_elements(soup: BeautifulSoup) -> dict:
    """Count semantic HTML5 elements."""
    semantic_tags = ["article", "nav", "main", "section", "aside", "header", "footer", "figure", "figcaption", "details", "summary", "time", "mark"]
    counts = {}
    for tag in semantic_tags:
        count = len(soup.find_all(tag))
        if count > 0:
            counts[tag] = count
    return counts


def _compute_stats(html_content: str, soup: BeautifulSoup, agent_view: dict) -> dict:
    """Compute summary statistics."""
    all_elements = soup.find_all(True)
    total = len(all_elements)

    # Text-to-code ratio
    text = agent_view["text_content"]
    text_ratio = len(text) / max(len(html_content), 1)

    # Images with alt
    images = agent_view["images"]
    images_with_alt = sum(1 for img in images if img["has_alt"])

    # Semantic HTML score (0-10)
    semantic = agent_view["semantic_elements"]
    semantic_score = min(10, len(semantic) * 2)  # 2 points per unique semantic element type, max 10

    # Heading hierarchy check
    headings = agent_view["headings"]
    has_h1 = any(h["level"] == 1 for h in headings)
    heading_levels = [h["level"] for h in headings]
    proper_hierarchy = has_h1 and heading_levels == sorted(heading_levels) if headings else False

    return {
        "total_elements": total,
        "text_length": len(text),
        "text_to_html_ratio": round(text_ratio, 3),
        "js_dependent_count": len(agent_view["js_dependent_elements"]),
        "structured_data_found": len(agent_view["structured_data"]) > 0,
        "structured_data_count": len(agent_view["structured_data"]),
        "semantic_html_score": semantic_score,
        "semantic_elements_used": list(semantic.keys()),
        "heading_count": len(headings),
        "has_h1": has_h1,
        "proper_heading_hierarchy": proper_hierarchy,
        "link_count": len(agent_view["links"]),
        "image_count": len(images),
        "images_with_alt": images_with_alt,
        "images_without_alt": len(images) - images_with_alt,
        "form_count": len(agent_view["forms"]),
        "meta_tags_found": list(agent_view["meta"].keys()),
    }
