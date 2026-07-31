"""

Single source of truth for Market Advisor versioning.



Bump rules (semver):

  MAJOR — breaking behavior / incompatible settings

  MINOR — new features, UI overhauls, meaningful trading-logic changes

  PATCH — fixes, polish, small tweaks



Update __version__ here whenever we ship a coherent batch of work.

"""



APP_NAME = "Market Advisor"

APP_NAME_COMPACT = "MarketAdvisor"

APP_TAGLINE = "Multi-Broker Quantitative Platform"



# Current release

__version__ = "1.2.8"



# Optional human note (shown in About / logs — keep short)

VERSION_NOTE = "Upcoming IPO calendar (advisory — RH IPO Access is manual)"



def version_string():

    """e.g. '1.1.0'"""

    return __version__



def display_name():

    """e.g. 'Market Advisor 1.1.0'"""

    return f"{APP_NAME} {__version__}"



def window_title():

    """Main window title bar."""

    return f"{APP_NAME_COMPACT} v{__version__} — {APP_TAGLINE}"



def user_agent():

    """HTTP User-Agent for Discord / monitor."""

    return f"{APP_NAME_COMPACT}/{__version__}"



def splash_subtitle():

    return f"v{__version__}"

