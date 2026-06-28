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

def chunk_text(text: str, max_words: int = 400, overlap_words: int = 40) -> List[str]:
    """Split text into semantic chunks, preserving paragraph boundaries where possible."""
    import re
    # Split by double newlines to get paragraphs
    paragraphs = re.split(r'\n\s*\n', text)
    
    chunks = []
    current_chunk = []
    current_word_count = 0
    
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
            
        para_word_count = len(para.split())
        
        # If the new paragraph pushes us over the limit, yield the current chunk
        if current_word_count + para_word_count > max_words and current_chunk:
            chunks.append("\n\n".join(current_chunk))
            # Try to keep the last paragraph as overlap if it isn't the entire chunk
            if len(current_chunk) > 1 and len(current_chunk[-1].split()) <= overlap_words * 2:
                current_chunk = [current_chunk[-1]]
                current_word_count = len(current_chunk[0].split())
            else:
                current_chunk = []
                current_word_count = 0
        
        # Now handle the incoming paragraph
        if para_word_count > max_words:
            # If the paragraph itself is massive, fall back to naive word splitting
            words = para.split()
            i = 0
            while i < len(words):
                chunks.append(" ".join(words[i:i + max_words]))
                i += max_words - overlap_words
            current_chunk = []
            current_word_count = 0
        else:
            current_chunk.append(para)
            current_word_count += para_word_count
            
    if current_chunk:
        chunks.append("\n\n".join(current_chunk))
        
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
