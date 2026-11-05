"""BTC market-data layer: provider series, websocket feeds, feed health, Parquet recording.

Submodules are imported explicitly (``from btc5m_bot.data.series import BtcSeries``) so that
importing the package stays cheap and does not pull in heavy optional dependencies.
"""
