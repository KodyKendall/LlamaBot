from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from dotenv import load_dotenv
load_dotenv()

from langgraph.graph import MessagesState
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from langgraph.graph import START, StateGraph
from langgraph.prebuilt import tools_condition
from langgraph.prebuilt import ToolNode

import asyncio

from app.agents.utils.playwright_screenshot import capture_page_and_img_src

from openai import OpenAI
from app.agents.utils.images import encode_image

@tool
def write_html(html_code: str) -> str:
    """
    Write HTML code to a file.
    """
    with open("/Users/kodykendall/SoftEngineering/LLMPress/Simple/LlamaBotSimple/page.html", "w") as f:
        f.write(html_code)
    return "HTML code written to page.html"

@tool
def write_css(css_code: str) -> str:
    """
    Write CSS code to a file.
    """
    with open("/Users/kodykendall/SoftEngineering/LLMPress/Simple/LlamaBotSimple/assets/page.css", "w") as f:
        f.write(css_code)
    return "CSS code written to page.css"

@tool
def write_javascript(javascript_code: str) -> str:
    """
    Write JavaScript code to a file.
    """
    with open("/Users/kodykendall/SoftEngineering/LLMPress/Simple/LlamaBotSimple/assets/page.js", "w") as f:
        f.write(javascript_code)
    return "JavaScript code written to page.js"


@tool
def get_screenshot_and_html_content_using_playwright(url: str) -> tuple[str, list[str]]:
    """
    Get the screenshot and HTML content of a webpage using Playwright. Then, generate the HTML as a clone, and save it to the file system. 
    """
    trimmed_html_content, image_sources = asyncio.run(capture_page_and_img_src(url, "assets/screenshot-of-page-to-clone.png"))
    
    if not trimmed_html_content:
        return "Screenshot functionality is not available. Please install Playwright to enable this feature."

    llm = ChatOpenAI(model="o3")

    # Getting the Base64 string
    base64_image = encode_image("assets/screenshot-of-page-to-clone.png")

    print(f"Making our call to o3 vision right now")
    
    response = llm.invoke([
        SystemMessage(content="""
            ### SYSTEM
You are "Pixel-Perfect Front-End", a senior web-platform engineer who specialises in
 * redesigning bloated, auto-generated pages into clean, semantic, WCAG-conformant HTML/CSS
 * matching the *visual* layout of the reference screenshot to within ±2 px for all major breakpoints

When you reply you MUST:
1. **Think step-by-step silently** ("internal reasoning"), then **output nothing but the final HTML inside a single fenced code block**.
2. **Inline zero commentary** – the code block is the entire answer.
3. Use **only system fonts** (font-stack: `Roboto, Arial, Helvetica, sans-serif`) and a single `<style>` block in the `<head>`.
4. Avoid JavaScript unless explicitly asked; replicate all interactions with pure HTML/CSS where feasible.
5. Preserve all outbound links exactly as provided in the RAW_HTML input.
7. Ensure the layout is mobile-first responsive (Flexbox/Grid) and maintains the same visual hierarchy:  
   e.g) **header ➔ main (logo, search box, buttons, promo) ➔ footer**.

### USER CONTEXT
You will receive two payloads:

**SCREENSHOT** – a screenshot of the webpage.  
**RAW_HTML** – the stripped, uglified DOM dump (may include redundant tags, hidden dialogs, etc.).

### TASK
1. **Infer the essential visual / UX structure** of the page from SCREENSHOT.  
2. **Cross-reference** with RAW_HTML only to copy:
   * anchor `href`s & visible anchor text
   * any aria-labels, alt text, or titles that improve accessibility.
3. **Discard** every element not visible in the screenshot (menus, dialogs, split-tests, inline JS blobs).
4. Re-create the page as a **single HTML document** following best practices described above.

### OUTPUT FORMAT
Return one fenced code block starting with <!DOCTYPE html> and ending with </html>
No extra markdown, no explanations, no leading or trailing whitespace outside the code block.
             
             Here is the trimmed down HTML:
             {trimmed_html_content}
        """),
        HumanMessage(content=f"Here is the trimmed down HTML: {trimmed_html_content}"),
        HumanMessage(content=[
            {"type": "text", "text": "Please clone this webpage based on the screenshot and HTML content provided."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
        ])
    ])

    with open("/Users/kodykendall/SoftEngineering/LLMPress/Simple/LlamaBotSimple/page.html", "w") as f:
        f.write(response.content)
    
    return "Cloned webpage written to file"

# Global tools list
tools = [write_html, write_css, write_javascript, get_screenshot_and_html_content_using_playwright]

# System message
sys_msg = SystemMessage(content="You are a helpful software_developer_assistant tasked with writing HTML, CSS, and JavaScript code to files. HTML is always written first, then CSS, then JavaScript. CSS will be written to assets/page.css and JavaScript will be written to assets/page.js, reference them accordingly in your generated code. If you are cloning a webpage, you will just write the final output directly into the single HTML page including the CSS and JavaScript in that single file.")

# Node
def software_developer_assistant(state: MessagesState):
   llm = ChatOpenAI(model="o4-mini")
   llm_with_tools = llm.bind_tools(tools)
   return {"messages": [llm_with_tools.invoke([sys_msg] + state["messages"])]}

def build_workflow(checkpointer=None):
    # Graph
    builder = StateGraph(MessagesState)

    # Define nodes: these do the work
    builder.add_node("software_developer_assistant", software_developer_assistant)
    builder.add_node("tools", ToolNode(tools))

    # Define edges: these determine how the control flow moves
    builder.add_edge(START, "software_developer_assistant")
    builder.add_conditional_edges(
        "software_developer_assistant",
        # If the latest message (result) from software_developer_assistant is a tool call -> tools_condition routes to tools
        # If the latest message (result) from software_developer_assistant is a not a tool call -> tools_condition routes to END
        tools_condition,
    )
    builder.add_edge("tools", "software_developer_assistant")
    react_graph = builder.compile(checkpointer=checkpointer)

    return react_graph