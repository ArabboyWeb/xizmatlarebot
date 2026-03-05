from .converter import router as converter_router
from .currency import router as currency_router
from .pollinations import router as pollinations_router
from .rembg import router as rembg_router
from .shazam import router as shazam_router
from .tempmail import router as tempmail_router
from .tinyurl import router as tinyurl_router
from .translate import router as translate_router
from .weather import router as weather_router
from .wikipedia import router as wikipedia_router

__all__ = [
    "converter_router",
    "currency_router",
    "pollinations_router",
    "rembg_router",
    "shazam_router",
    "tempmail_router",
    "tinyurl_router",
    "translate_router",
    "weather_router",
    "wikipedia_router",
]
