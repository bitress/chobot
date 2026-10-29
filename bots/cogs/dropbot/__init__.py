"""
DropBot, split by layer (each module only imports from the ones above it):

  constants       Constants, Sanrio villager data and the access-denial result type.
  state           Process-wide in-memory guards: background tasks, cooldowns, drop locks, the shared HTTP session.
  db              Synchronous database helpers (call through asyncio.to_thread): panels, audit logs, last drop, wizard state, carts.
  islands         Island-name helpers and access checks (blocklist, sub/order rules).
  parsing         Drop-input parsing, villager name matching and catalog search.
  status          Live island status, dodo lookup/webhook, and the embeds built from them.
  ui_base         Shared view plumbing: the owner-only base view, ephemeral reply helpers.
  actions         Shared actions behind every drop/injection entry point (_perform_drop, _perform_injection, guardrails).
  cart            The drop cart: persistent cart model, summary, and item colour/pattern variants.
  views_villager  Villager injection UI: pickers, confirm views, Sanrio wizard and the guarded _start_injection.
  views_catalog   Catalog search, item detail, cart manager and the drop pickers.
  panel           Persistent island panel: DynamicItem buttons and the panel view.
  cog             The DropBot cog: panel refresh loop and slash commands.

The cog is still loaded through ``bots.cogs.drop_bot_cog``, which re-exports everything here.
"""
