"""
DropBot Cog — Integrated island control panels for Discord.

Provides persistent, interaction-only UI panels in sub-island channels:
  • 🦤 Get Dodo        – fetches the live Dodo from SysBot, sends it ephemerally, logs to webhook
  • 📦 Drop Items      – modal for custom item/hex code drops (validated)
  • 🔍 Search Catalog  – browse the ACNH catalog, build a drop cart (pick item colors/patterns,
                         which re-computes the dropped hex, or set a custom hex), inject villagers
  • 🐱 Inject Villager – modal with fuzzy villager matching and a confirm step
  • 📖 Guide           – ephemeral how-to for the island panel
  • 🌐 Island Page     – link button to the island's page on chopaeng.com

The panel embed auto-refreshes every 30s (status, clock, villagers and visitors).

Admin slash commands (under /dropbot group, Manage Messages required):
  • /dropbot setup-panel [island]  – post/refresh panel in current channel
  • /dropbot setup-all             – bulk-deploy panels under the sub-island category
  • /dropbot status                – list all registered panels
Anyone:
  • /catalog and /dropbot catalog  – catalog search (the island option is restricted to
                                      islands whose channel the user can see)

Requires ZERO privileged gateway intents.

Persistence: every panel button is a discord.ui.DynamicItem whose custom_id
carries the island (``dropbot:<action>:<island>``). The item classes are
registered once in cog_load(), so buttons keep working after a restart without
re-adding a view per message. Requires discord.py >= 2.4.
"""

from __future__ import annotations

# The implementation lives in the ``bots.cogs.dropbot`` package (see its __init__ for the module map).
# This module re-exports every name that used to be defined here so existing imports keep working.
#
# Note for tests: ``unittest.mock.patch`` must target the module that *uses* a name, e.g.
# ``bots.cogs.dropbot.actions._access_denied``. Patching it here would not affect the code.
from .dropbot.constants import (  # noqa: F401
    ACCESS_CACHE_TTL,
    ACTION_COOLDOWN,
    CART_SAVE_DELAY,
    CART_TTL_SECONDS,
    CATALOG_ITEM_LIMIT,
    DODO_GIF,
    Denied,
    ENFORCE_BLOCKLIST,
    FORCE_EDIT_SECONDS,
    GUILD_ID,
    LOGO_URL,
    MAP_CDN_BASE,
    MAX_DROP_SLOTS,
    MAX_ITEM_NAME,
    MAX_STACK,
    ORDER_RULES_MSG,
    ORDER_RULES_ROLE_ID,
    PANEL_EMBED_COLOR,
    PANEL_EMBED_COLOR_ERR,
    PANEL_EMBED_COLOR_WARN,
    PANEL_FOOTER_ICON,
    PANEL_FOOTER_TEXT,
    PLACEHOLDER_VILLAGERS,
    PLOT_MAX,
    PLOT_MIN,
    REFRESH_CONCURRENCY,
    REQUIRE_REGISTERED_CHARACTER,
    SANRIO_EMBED_COLOR,
    SANRIO_NAMES_MAP,
    SANRIO_VILLAGERS,
    SANRIO_VILLAGER_CODES,
    SUB_RULES_CHANNEL_ID,
    SUB_RULES_MSG,
    SUB_RULES_ROLE_ID,
    UNKNOWN_ISLAND_MSG,
    WEBHOOK_DEBOUNCE_TTL,
    WEBSITE_BASE,
    _PLACEHOLDER_DODOS,
    _build_sub_rules_msg,
    get_canonical_sanrio_name,
    is_sanrio_villager,
)
from .dropbot.state import (  # noqa: F401
    DROP_SUBMIT_TIMEOUT,
    MAX_DROP_QUEUE,
    _ACCESS_CACHE,
    _BACKGROUND_TASKS,
    _COOLDOWNS,
    _HTTP_SESSION,
    _ISLAND_DROP_LOCKS,
    _ISLAND_DROP_WAITING,
    _PANEL_CHANNELS_CACHE,
    _WEBHOOK_DEBOUNCE,
    _close_http,
    _cooldown_eta_ts,
    _cooldown_left,
    _debounced,
    _http,
    _island_drop_lock,
    _pop_cooldown,
    _spawn,
)
from .dropbot.db import (  # noqa: F401
    LAST_DROP_CACHE_MAX,
    LAST_DROP_MISS_TTL,
    _AUDIT_ITEMS_MAX,
    _LAST_DROP_MISSES,
    _USER_LAST_DROPS,
    _clear_sanrio_wizard_state,
    _fetch_last_drop_from_db,
    _get_panel_row,
    _get_user_last_drop,
    _init_tables,
    _load_all_panels,
    _load_cart_rows,
    _load_sanrio_wizard_state,
    _load_user_last_drop,
    _log_drop,
    _log_injection,
    _panel_channel_ids,
    _panel_channel_ids_cached,
    _peek_user_last_drop,
    _record_user_drop,
    _save_cart_rows,
    _save_sanrio_wizard_state,
    _upsert_panel,
)
from .dropbot.islands import (  # noqa: F401
    _ISLAND_MAP,
    _ISLAND_MAP_KEY,
    _access_denied,
    _canonical_island,
    _check_access,
    _denied_reason,
    _has_order_role,
    _has_registered_character,
    _has_sub_role,
    _infer_island,
    _infer_island_from_channel,
    _interactive_map_url,
    _island_map,
    _island_url,
    _norm,
    _sub_category_id,
    _user_can_target,
)
from .dropbot.parsing import (  # noqa: F401
    _ENTRY_RE,
    _ITEM_NAME_RE,
    _VILLAGER_NAMES,
    _VILLAGER_NAMES_LOADED,
    _format_item_for_sysbot,
    _get_villager_names,
    _match_villagers,
    _normalize_items,
    _search_catalog,
    build_drop_payload,
    parse_drop_items,
)
from .dropbot.status import (  # noqa: F401
    _CONSOLE_API_CACHE,
    _CONSOLE_API_LOCK,
    _DODO_SOURCES,
    _ISLAND_DB_INFO_CACHE,
    _ISLAND_STATUS_CACHE,
    _MAP_CACHE,
    _PRESENCE_LINE_CACHE,
    _build_guide_embed,
    _build_panel_embed,
    _bullets,
    _clean_dodo,
    _clean_key,
    _dodo_from_direct,
    _dodo_from_disk,
    _dodo_from_snapshot,
    _dodo_from_status,
    _dodo_from_telemetry,
    _dodo_webhook_url,
    _embed_signature,
    _err_embed,
    _find_dodo,
    _fire_dropbot_dodo_webhook,
    _get_console_api_islands,
    _get_island_db_info,
    _get_island_db_info_async,
    _lookup_saved_character,
    _map_url_async,
    _persist_dodo_reveal_message,
    _presence_line,
    _read_dodo_file,
    _safe_call,
    _update_dodo_reveal_url,
    build_drop_picker_embed,
    build_villager_picker_embed,
    fetch_island_live_status,
)
from .dropbot.ui_base import (  # noqa: F401
    SubRulesPromptView,
    _OwnedView,
    _adopt,
    _reply,
    _send,
)
from .dropbot.actions import (  # noqa: F401
    _Resident,
    _check_island_online,
    _fetch_residents,
    _injection_guard,
    _is_failure,
    _parse_residents,
    _perform_drop,
    _perform_injection,
    _resolve_client,
    _sanitize_error_message,
)
from .dropbot.cart import (  # noqa: F401
    POCKET_SLOTS,
    _CartEntry,
    _HEX_INPUT_RE,
    _NO_PATTERN_LABEL,
    _PATTERN_VARIANT_RE,
    _PersistentCart,
    _Variant,
    _cart_key,
    _cart_summary,
    _clean_variant_text,
    _default_qty,
    _item_variants,
    _match_variant_index,
    _restore_cart_entries,
    _stack_size,
    _unique,
)
from .dropbot.views_villager import (  # noqa: F401
    CustomAmiiboModal,
    CustomPlaceholderModal,
    InjectVillagerModal,
    InjectionConfirmView,
    InteractiveVillagerPickerView,
    SanrioInjectionWizardView,
    VillagerChoiceView,
    VillagerPlotPickView,
    VillagerSearchModal,
    _start_injection,
)
from .dropbot.views_catalog import (  # noqa: F401
    CURATED_POCKET_BUNDLES,
    CartEditModal,
    CartManagerView,
    CatalogSearchModal,
    CatalogSearchView,
    CustomDropModal,
    DropFollowupView,
    InteractiveDropPickerView,
    ItemDetailView,
    ItemHexModal,
    POPULAR_CATALOG_SERIES,
    PresetDropConfirmView,
    RetryDropView,
    SavedPocketPickerView,
    SeriesFilterView,
    _get_pocket_bundles,
    _get_popular_catalog_series,
    _show_catalog,
)
from .dropbot.panel import (  # noqa: F401
    ALL_REGISTERED_DYNAMIC_ITEMS,
    DropItemsButton,
    GetDodoButton,
    GuideButton,
    InjectVillagerButton,
    IslandPanelView,
    PANEL_ITEMS,
    RefreshButton,
    SearchCatalogButton,
    _IN_FLIGHT_DODO_LOOKUPS,
    _IslandButton,
    _cid,
    _find_dodo_coalesced,
)
from .dropbot.cog import (  # noqa: F401
    DropBotCog,
    _PanelSig,
)
