"""
Parsers d'emails d'alerte, un par site.
Chaque parser prend un RawEmail et retourne une liste de Listing.
"""
from .leboncoin import parse as parse_leboncoin
from .lacentrale import parse as parse_lacentrale
from .autoscout24 import parse as parse_autoscout24


PARSERS = {
    "leboncoin": parse_leboncoin,
    "lacentrale": parse_lacentrale,
    "autoscout24": parse_autoscout24,
}
