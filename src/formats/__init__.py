"""Public localization-format metadata API."""

from src.localization_adapters import (
    JSON_ADAPTER,
    JAVA_PROPERTIES_ADAPTER,
    LocalizationFileAdapter,
    get_localization_adapter,
    list_localization_adapters,
    register_localization_adapter,
    unregister_localization_adapter,
)
from src.localization_formats import (
    JSON_FORMAT,
    JAVA_PROPERTIES_FORMAT,
    LocalizationFormat,
    list_localization_formats,
    load_localization_format,
    register_localization_format,
    unregister_localization_format,
)
from src.localization_layouts import (
    LOCALE_DIRECTORY_LAYOUT,
    LOCALE_FILENAME_LAYOUT,
    SUFFIX_LAYOUT,
    LocalizationLayout,
    load_localization_layout,
)
from src.localization_profiles import (
    LocalizationProfile,
    load_localization_profiles,
)

__all__ = [
    "JSON_ADAPTER",
    "JSON_FORMAT",
    "JAVA_PROPERTIES_ADAPTER",
    "JAVA_PROPERTIES_FORMAT",
    "LOCALE_DIRECTORY_LAYOUT",
    "LOCALE_FILENAME_LAYOUT",
    "LocalizationFileAdapter",
    "LocalizationFormat",
    "LocalizationLayout",
    "LocalizationProfile",
    "SUFFIX_LAYOUT",
    "get_localization_adapter",
    "list_localization_adapters",
    "list_localization_formats",
    "load_localization_format",
    "load_localization_layout",
    "load_localization_profiles",
    "register_localization_adapter",
    "register_localization_format",
    "unregister_localization_adapter",
    "unregister_localization_format",
]
