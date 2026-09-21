"""Optional browser evasion: stealth scripts, proxies, fingerprints.

Toggle via settings (`evasion.enabled`) or the Evasion sidebar tool.
When disabled, browsers launch exactly as before — no proxies, no stealth.
"""

from raindance.evasion.browser_factory import BrowserFactory
from raindance.evasion.fingerprint_manager import FingerprintManager
from raindance.evasion.proxy_manager import ProxyManager
from raindance.evasion.proxy_groups import ProxyGroupManager
from raindance.evasion.proxy_inventory import ProxyInventory

__all__ = ["BrowserFactory", "FingerprintManager", "ProxyManager",
           "ProxyGroupManager", "ProxyInventory"]
