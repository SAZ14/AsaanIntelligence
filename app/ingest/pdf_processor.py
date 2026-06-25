import os
import io
import httpx
from pypdf import PdfReader
from typing import List, Dict, Any

def download_twilio_media(media_url: str) -> bytes:
    """Download media from Twilio, handling redirects and stripping auth on external hosts."""
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    
    # 1. Try public access first (default for Twilio Sandbox)
    with httpx.Client(follow_redirects=True) as client:
        try:
            resp = client.get(media_url)
            if resp.status_code == 200:
                return resp.content
        except Exception:
            pass

    # 2. Try with auth if public access failed, stripping auth on external redirects (e.g. S3)
    from urllib.parse import urlparse
    url = media_url
    auth = (sid, token) if (sid and token) else None
    
    with httpx.Client(follow_redirects=False) as client:
        for _ in range(5):
            resp = client.get(url, auth=auth)
            if resp.status_code in (301, 302, 307, 308):
                location = resp.headers["Location"]
                # Resolve relative redirects
                if location.startswith("/"):
                    parsed_orig = urlparse(url)
                    url = f"{parsed_orig.scheme}://{parsed_orig.netloc}{location}"
                else:
                    url = location
                
                # Strip auth if redirecting to a different host
                if urlparse(url).netloc != urlparse(media_url).netloc:
                    auth = None
            elif resp.status_code == 200:
                return resp.content
            else:
                resp.raise_for_status()
                
    raise Exception("Failed to download media from Twilio after redirects.")

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract text from a PDF byte array."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = ""
    for page in reader.pages:
        extracted = page.extract_text()
        if extracted:
            text += extracted + "\n\n"
    return text

def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> List[str]:
    """Split text into overlapping chunks of approximate word counts."""
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i:i + chunk_size])
        chunks.append(chunk)
        i += chunk_size - overlap
    return chunks

def process_and_store_pdf(media_url: str, filename: str) -> int:
    """
    Downloads a PDF, extracts text, chunks it, embeds it, and stores it in Supabase.
    Returns the number of chunks processed.
    """
    pdf_bytes = download_twilio_media(media_url)
    text = extract_text_from_pdf(pdf_bytes)
    
    if not text.strip():
        return 0
        
    chunks = chunk_text(text)
    
    from app.community.store import store_knowledge_chunks, clear_knowledge_by_source
    
    # Clear old chunks for this document to prevent zombie knowledge
    clear_knowledge_by_source(filename)
    
    # Prepare chunk metadata
    documents = []
    for chunk in chunks:
        documents.append({
            "content": chunk,
            "metadata": {"source": filename}
        })
        
    store_knowledge_chunks(documents)
    return len(chunks)
