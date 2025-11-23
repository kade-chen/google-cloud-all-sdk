import json
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional


@dataclass
class BaseInfo:
    userId: Optional[str] = None
    token: Optional[str] = None
    longitude: Optional[float] = None
    latitude: Optional[float] = None
    voice: Optional[str] = None
    location: Optional[str] = None
    date: Optional[datetime] = None

    def to_json(self, indent: int = 2, ensure_ascii: bool = False) -> str:
        def default_converter(o):
            if isinstance(o, datetime):
                return o.strftime("%Y-%m-%d %H:%M:%S")
            return str(o)

        return json.dumps(asdict(self), ensure_ascii=ensure_ascii, indent=indent, default=default_converter)