import requests
import time
import random
import logging
import os
import json
import sys
from urllib.parse import urlparse
import asyncio
import aiohttp
from aiohttp_socks import ProxyConnector
import telegram
from telegram.error import TelegramError
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("username_checker.log"),
        logging.StreamHandler()
    ]
)

# Configuration
TELEGRAM_BOT_TOKEN = "8156910065:AAEypnnghXjH2kFE0VqHr7lTd8JHs3eE61Y"  # Replace with your bot token
TELEGRAM_CHAT_ID = "7562336465"  # Replace with the recipient's chat ID
PROXY_FILE = "proxies.txt"
USERNAMES_FILE = "usernames.txt"
RESULTS_FILE = f"results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
CHECKPOINT_FILE = "checkpoint.json"
MAX_WORKERS = 100  # Number of concurrent connections
REQUESTS_PER_PROXY = 15  # Rotate proxy after this many requests
REQUEST_TIMEOUT = 5  # Seconds
DELAY_BETWEEN_REQUESTS = (0.1, 0.3)  # Random delay range in seconds
MAX_RETRIES = 3  # Maximum number of retries for a username
BATCH_SIZE = 200  # Number of usernames to process in a batch

class InstagramUsernameChecker:
    def __init__(self):
        self.proxies = []
        self.working_proxies = []
        self.failing_proxies = {}  # Track failing proxies with timestamp
        self.proxy_usage_count = {}  # Track usage count for each proxy
        self.proxy_lock = asyncio.Lock()
        self.result_lock = asyncio.Lock()
        self.bot = None
        self.session = None
        
        # Stats
        self.successful_checks = 0
        self.failed_checks = 0
        self.proxy_errors = 0
        self.untaken_usernames = []
        
        # Checkpointing
        self.processed_usernames = set()
        self.progress = 0
        
        # User agents
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.2 Safari/605.1.15",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 15_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.2 Mobile/15E148 Safari/604.1",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:95.0) Gecko/20100101 Firefox/95.0",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.93 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/97.0.4692.71 Safari/537.36 Edg/97.0.1072.62",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36 OPR/82.0.4227.44",
            "Mozilla/5.0 (iPad; CPU OS 15_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/96.0.4664.116 Mobile/15E148 Safari/604.1",
            "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:95.0) Gecko/20100101 Firefox/95.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:91.0) Gecko/20100101 Firefox/91.0"
        ]
        
    async def initialize(self):
        # Load proxies
        if not os.path.exists(PROXY_FILE):
            logging.error(f"Proxy file {PROXY_FILE} not found!")
            return False
            
        with open(PROXY_FILE, "r") as f:
            self.proxies = [line.strip() for line in f if line.strip()]
            
        if not self.proxies:
            logging.error("No valid proxies found in the proxy file!")
            return False
            
        # Initialize working proxies list
        self.working_proxies = self.proxies.copy()
        
        # Load checkpoint if it exists
        if os.path.exists(CHECKPOINT_FILE):
            try:
                with open(CHECKPOINT_FILE, "r") as f:
                    checkpoint_data = json.load(f)
                    self.processed_usernames = set(checkpoint_data.get("processed_usernames", []))
                    self.untaken_usernames = checkpoint_data.get("untaken_usernames", [])
                    self.successful_checks = checkpoint_data.get("successful_checks", 0)
                    self.failed_checks = checkpoint_data.get("failed_checks", 0)
                    self.progress = checkpoint_data.get("progress", 0)
                    
                logging.info(f"Loaded checkpoint: {len(self.processed_usernames)} usernames already processed")
            except Exception as e:
                logging.error(f"Error loading checkpoint: {e}")
        
        logging.info(f"Loaded {len(self.proxies)} proxies")
        
        # Initialize Telegram bot
        try:
            self.bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
            await self.bot.get_me()  # Test the connection
            logging.info("Telegram bot initialized successfully")
        except TelegramError as e:
            logging.error(f"Failed to initialize Telegram bot: {e}")
            return False
        
        # Test proxies in parallel to find the working ones
        logging.info("Testing proxies for functionality...")
        await self.test_proxies()
        
        if not self.working_proxies:
            logging.error("No working proxies found! Please check your proxy list.")
            return False
            
        logging.info(f"{len(self.working_proxies)} proxies are working correctly")
            
        return True
        
    async def test_proxies(self):
        """Test all proxies in parallel to find working ones"""
        test_url = "https://www.instagram.com/"
        test_tasks = []
        
        async def test_proxy(proxy_str):
            try:
                connector = self._create_connector(proxy_str)
                if not connector:
                    return False
                
                timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                    headers = {"User-Agent": random.choice(self.user_agents)}
                    async with session.get(test_url, headers=headers) as response:
                        return 200 <= response.status < 300
            except Exception:
                return False
        
        # Create tasks for testing each proxy
        for proxy in self.proxies:
            task = asyncio.create_task(test_proxy(proxy))
            test_tasks.append((proxy, task))
        
        # Process results
        self.working_proxies = []
        for proxy, task in test_tasks:
            try:
                is_working = await task
                if is_working:
                    self.working_proxies.append(proxy)
            except Exception:
                pass
                
    def _create_connector(self, proxy_str):
        """Create a proxy connector for aiohttp from a proxy string"""
        try:
            # Handle different proxy formats
            if "://" in proxy_str:
                proxy_type, proxy_addr = proxy_str.split("://", 1)
                if proxy_type.lower() == "http":
                    return aiohttp.TCPConnector(ssl=False)  # For HTTP proxies, we'll set the proxy in the request
                elif proxy_type.lower() in ["socks4", "socks5"]:
                    return ProxyConnector.from_url(proxy_str)
            else:
                # Default to http if no protocol specified
                return aiohttp.TCPConnector(ssl=False)
        except Exception as e:
            logging.error(f"Error creating connector for proxy {proxy_str}: {e}")
            return None
        
    async def get_working_proxy(self):
        """Get a working proxy with proper rotation"""
        async with self.proxy_lock:
            # If no working proxies, try to recover some from the failing list
            if not self.working_proxies:
                now = time.time()
                recovered = []
                for proxy, timestamp in list(self.failing_proxies.items()):
                    # If proxy has been in failing state for more than 5 minutes, try to recover
                    if now - timestamp > 300:
                        recovered.append(proxy)
                        del self.failing_proxies[proxy]
                
                if recovered:
                    self.working_proxies.extend(recovered)
                    logging.info(f"Recovered {len(recovered)} proxies from failing state")
                else:
                    # No proxies available at all
                    logging.critical("No working proxies available! Attempting to continue with original list.")
                    self.working_proxies = self.proxies.copy()
            
            # Get the least used proxy
            proxy_counts = [(p, self.proxy_usage_count.get(p, 0)) for p in self.working_proxies]
            proxy_counts.sort(key=lambda x: x[1])  # Sort by usage count
            
            if not proxy_counts:
                logging.error("No proxies available after sorting!")
                return None, None
                
            proxy_str = proxy_counts[0][0]
            
            # Update usage count
            self.proxy_usage_count[proxy_str] = self.proxy_usage_count.get(proxy_str, 0) + 1
            
            # If proxy has been used too many times, move it to the end of the rotation
            if self.proxy_usage_count[proxy_str] >= REQUESTS_PER_PROXY:
                self.proxy_usage_count[proxy_str] = 0
            
            # Format proxy for aiohttp
            connector = self._create_connector(proxy_str)
            return proxy_str, connector
    
    async def mark_proxy_as_failing(self, proxy_str):
        """Mark a proxy as failing and remove it from working proxies"""
        async with self.proxy_lock:
            if proxy_str in self.working_proxies:
                self.working_proxies.remove(proxy_str)
                self.failing_proxies[proxy_str] = time.time()
                self.proxy_errors += 1
                logging.warning(f"Marked proxy {proxy_str} as failing. {len(self.working_proxies)} working proxies remaining.")
    
    async def check_username(self, username):
        """Check if a username is available on Instagram using proxies."""
        username = username.strip()
        if not username:
            return None
            
        # Skip if already processed
        if username in self.processed_usernames:
            return None
            
        url = f"https://www.instagram.com/{username}/"
        
        headers = {
            "User-Agent": random.choice(self.user_agents),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
            "Referer": "https://www.instagram.com/",
            "cookie": ""  # Empty cookie to appear more like a browser
        }
        
        for attempt in range(MAX_RETRIES):
            try:
                proxy_str, connector = await self.get_working_proxy()
                if not connector:
                    await asyncio.sleep(random.uniform(1, 2))
                    continue
                    
                # Create a new session for this request
                timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                
                proxy_dict = None
                if "://" in proxy_str and proxy_str.split("://")[0].lower() == "http":
                    # For HTTP proxies
                    proxy_dict = proxy_str
                    
                # Allow redirects to properly handle 302 responses
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                    # First try with allow_redirects=False to see the initial response
                    async with session.get(url, headers=headers, proxy=proxy_dict, allow_redirects=False) as response:
                        status = response.status
                        
                        # If we get a redirect, we should check the redirect location
                        if status in [301, 302, 303, 307, 308]:
                            location = response.headers.get('Location', '')
                            
                            # If redirected to login page, the username likely exists
                            if 'login' in location or 'accounts/login' in location:
                                async with self.result_lock:
                                    self.successful_checks += 1
                                    self.processed_usernames.add(username)
                                logging.info(f"Taken - ({username}) [Redirect to login]")
                                return ("taken", username)
                            
                            # Follow the redirect to see final destination
                            async with session.get(url, headers=headers, proxy=proxy_dict, allow_redirects=True) as redirect_response:
                                final_url = str(redirect_response.url)
                                final_status = redirect_response.status
                                
                                # Check if we landed on the username page (username exists)
                                if final_status == 200 and f"/{username}" in final_url:
                                    async with self.result_lock:
                                        self.successful_checks += 1
                                        self.processed_usernames.add(username)
                                    logging.info(f"Taken - ({username}) [After redirect]")
                                    return ("taken", username)
                                # Check if we ended up on a 404 page (username doesn't exist)
                                elif final_status == 404:
                                    async with self.result_lock:
                                        self.successful_checks += 1
                                        self.untaken_usernames.append(username)
                                        self.processed_usernames.add(username)
                                    logging.info(f"Untaken - ({username}) [After redirect]")
                                    await self.send_telegram_message(f"✅ Untaken username found: {username}")
                                    return ("untaken", username)
                                # Other status codes after redirect
                                else:
                                    logging.info(f"Ambiguous result for {username}: Redirected to {final_url} with status {final_status}")
                                    if attempt == MAX_RETRIES - 1:
                                        # Consider it taken by default if we can't determine clearly
                                        async with self.result_lock:
                                            self.successful_checks += 1
                                            self.processed_usernames.add(username)
                                        return ("taken", f"{username} - Ambiguous redirect")
                        # Direct 200 response - username exists
                        elif status == 200:
                            async with self.result_lock:
                                self.successful_checks += 1
                                self.processed_usernames.add(username)
                            logging.info(f"Taken - ({username})")
                            return ("taken", username)
                        # Direct 404 response - username doesn't exist
                        elif status == 404:
                            async with self.result_lock:
                                self.successful_checks += 1
                                self.untaken_usernames.append(username)
                                self.processed_usernames.add(username)
                            logging.info(f"Untaken - ({username})")
                            await self.send_telegram_message(f"✅ Untaken username found: {username}")
                            return ("untaken", username)
                        # Rate limiting responses
                        elif status in [429, 403]:
                            await self.mark_proxy_as_failing(proxy_str)
                            if attempt == MAX_RETRIES - 1:
                                async with self.result_lock:
                                    self.failed_checks += 1
                                    self.processed_usernames.add(username)
                                return ("error", f"{username} - Rate limited")
                        else:
                            logging.warning(f"Unexpected status code {status} for {username} (attempt {attempt+1}/{MAX_RETRIES})")
                            if attempt == MAX_RETRIES - 1:
                                async with self.result_lock:
                                    self.failed_checks += 1
                                    self.processed_usernames.add(username)
                                return ("error", f"{username} - Status code: {status}")
                        
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if proxy_str:
                    await self.mark_proxy_as_failing(proxy_str)
                    
                logging.warning(f"Request error for {username}: {str(e)} (attempt {attempt+1}/{MAX_RETRIES})")
                if attempt == MAX_RETRIES - 1:
                    async with self.result_lock:
                        self.failed_checks += 1
                        self.processed_usernames.add(username)
                    return ("error", f"{username} - {str(e)}")
            except Exception as e:
                logging.error(f"Unexpected error checking {username}: {str(e)}")
                if attempt == MAX_RETRIES - 1:
                    async with self.result_lock:
                        self.failed_checks += 1
                        self.processed_usernames.add(username)
                    return ("error", f"{username} - Unexpected error")
                    
            # Wait before retrying with exponential backoff
            await asyncio.sleep(random.uniform(0.5, 1) * (attempt + 1))
                
        return None
    
    async def save_checkpoint(self):
        """Save current progress to checkpoint file"""
        checkpoint_data = {
            "processed_usernames": list(self.processed_usernames),
            "untaken_usernames": self.untaken_usernames,
            "successful_checks": self.successful_checks,
            "failed_checks": self.failed_checks,
            "progress": self.progress,
            "timestamp": datetime.now().isoformat()
        }
        
        try:
            with open(CHECKPOINT_FILE, "w") as f:
                json.dump(checkpoint_data, f)
            logging.debug("Checkpoint saved")
        except Exception as e:
            logging.error(f"Error saving checkpoint: {e}")
    
    async def send_telegram_message(self, message):
        """Send a message via Telegram bot."""
        if not self.bot:
            logging.error("Telegram bot not initialized!")
            return False
            
        try:
            await self.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
            return True
        except TelegramError as e:
            logging.error(f"Failed to send Telegram message: {e}")
            return False
    
    async def send_untaken_usernames_batch(self):
        """Send untaken usernames in batches to Telegram"""
        if not self.untaken_usernames:
            return
            
        # Send in batches of 10 to avoid message length limits
        batch_size = 10
        for i in range(0, len(self.untaken_usernames), batch_size):
            batch = self.untaken_usernames[i:i+batch_size]
            if batch:
                message = "🔎 UNTAKEN USERNAMES:\n\n" + "\n".join([f"• {username}" for username in batch])
                await self.send_telegram_message(message)
                # Short delay to avoid Telegram rate limits
                await asyncio.sleep(0.5)
    
    async def process_batch(self, usernames_batch):
        """Process a batch of usernames concurrently."""
        tasks = []
        semaphore = asyncio.Semaphore(MAX_WORKERS)  # Limit concurrent requests
        
        async def process_with_semaphore(username):
            async with semaphore:
                return await self.check_username(username)
        
        # Create tasks for all usernames in the batch
        for username in usernames_batch:
            task = asyncio.create_task(process_with_semaphore(username))
            tasks.append(task)
            
        # Wait for a small random delay to avoid sending all requests at once
        await asyncio.sleep(random.uniform(*DELAY_BETWEEN_REQUESTS))
            
        # Wait for all tasks to complete
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Filter out None results
        valid_results = [r for r in results if r is not None and not isinstance(r, Exception)]
        
        # Save results to file
        with open(RESULTS_FILE, "a") as f:
            for status, username_info in valid_results:
                f.write(f"{status},{username_info}\n")
                
        # Save checkpoint
        await self.save_checkpoint()
        
        # Return valid results
        return valid_results

    async def run(self):
        """Main function to run the username checker."""
        # Check if usernames file exists
        if not os.path.exists(USERNAMES_FILE):
            logging.error(f"Usernames file {USERNAMES_FILE} not found!")
            return False
            
        # Read usernames from file
        with open(USERNAMES_FILE, "r") as f:
            usernames = [line.strip() for line in f if line.strip()]
            
        if not usernames:
            logging.error("No valid usernames found in the file!")
            return False
            
        total_usernames = len(usernames)
        logging.info(f"Loaded {total_usernames} usernames to check")
        
        # Send startup message to Telegram
        await self.send_telegram_message(f"🚀 Instagram Username Checker started\n\nChecking {total_usernames} usernames...")
        
        # Filter out already processed usernames
        usernames_to_process = [u for u in usernames if u not in self.processed_usernames]
        logging.info(f"{len(usernames_to_process)} usernames left to process after filtering already processed ones")
        
        # Process in batches
        for i in range(0, len(usernames_to_process), BATCH_SIZE):
            batch = usernames_to_process[i:i+BATCH_SIZE]
            if not batch:
                continue
                
            logging.info(f"Processing batch {i//BATCH_SIZE + 1}/{(len(usernames_to_process) + BATCH_SIZE - 1) // BATCH_SIZE} ({len(batch)} usernames)")
            
            # Process the batch
            results = await self.process_batch(batch)
            
            # Update progress
            self.progress = (i + len(batch)) / len(usernames_to_process) * 100
            
            # Send status update to Telegram every 5 batches
            if (i // BATCH_SIZE) % 5 == 0 or i + BATCH_SIZE >= len(usernames_to_process):
                status_message = (
                    f"📊 Status Update\n\n"
                    f"• Progress: {self.progress:.1f}%\n"
                    f"• Checked: {self.successful_checks + self.failed_checks}/{total_usernames}\n"
                    f"• Untaken: {len(self.untaken_usernames)}\n"
                    f"• Success rate: {(self.successful_checks / (self.successful_checks + self.failed_checks) * 100) if (self.successful_checks + self.failed_checks) > 0 else 0:.1f}%\n"
                    f"• Working proxies: {len(self.working_proxies)}/{len(self.proxies)}\n"
                    f"• Proxy errors: {self.proxy_errors}"
                )
                await self.send_telegram_message(status_message)
                
                # Send untaken usernames batch
                await self.send_untaken_usernames_batch()
                # Clear the list after sending
                self.untaken_usernames = []
        
        # Final status update
        final_message = (
            f"✅ Checking complete!\n\n"
            f"• Total checked: {self.successful_checks + self.failed_checks}/{total_usernames}\n"
            f"• Total untaken: {len(self.untaken_usernames)}\n"
            f"• Success rate: {(self.successful_checks / (self.successful_checks + self.failed_checks) * 100) if (self.successful_checks + self.failed_checks) > 0 else 0:.1f}%\n"
            f"• Proxy errors: {self.proxy_errors}"
        )
        await self.send_telegram_message(final_message)
        
        # Send final batch of untaken usernames
        await self.send_untaken_usernames_batch()
        
        # Save final checkpoint
        await self.save_checkpoint()
        
        logging.info("Username checking process completed successfully")
        return True


async def main():
    checker = InstagramUsernameChecker()
    if await checker.initialize():
        await checker.run()
    else:
        logging.error("Failed to initialize the username checker")

if __name__ == "__main__":
    asyncio.run(main())