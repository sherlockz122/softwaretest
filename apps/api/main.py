from apps.api.application import create_app
from packages.platform.config import load_settings

app = create_app(load_settings())
