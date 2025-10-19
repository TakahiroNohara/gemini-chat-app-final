
import os
import requests
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class GoogleBooksClient:
    def __init__(self, api_key: Optional[str] = None, timeout: int = 10):
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not self.api_key:
            raise ValueError("Google Books API key is required.")
        self.base_url = "https://www.googleapis.com/books/v1/volumes"
        self.timeout = timeout

    def search_books(self, query: str, max_results: int = 10) -> List[Dict[str, Any]]:
        params = {
            "q": query,
            "key": self.api_key,
            "maxResults": max_results,
            "langRestrict": "ja",
        }
        try:
            response = requests.get(self.base_url, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            return self._normalize_results(data.get("items", []))
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching data from Google Books API: {e}")
            return []

    def _normalize_results(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized = []
        for item in items:
            volume_info = item.get("volumeInfo", {})
            normalized.append({
                "title": volume_info.get("title"),
                "authors": volume_info.get("authors", []),
                "publisher": volume_info.get("publisher"),
                "published_date": volume_info.get("publishedDate"),
                "description": volume_info.get("description"),
                "thumbnail": volume_info.get("imageLinks", {}).get("thumbnail"),
                "info_link": volume_info.get("infoLink"),
            })
        return normalized
