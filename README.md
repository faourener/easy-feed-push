# 🚀 Easy Feed Push

This is a fully automated news processing engine. It fetches the latest news at a high frequency based on configured RSS feeds and precisely filters them using dynamic keywords configured in Notion. The system saves the data to a local SQLite database and uses the DeepSeek Large Language Model to summarize key information from the past 24 hours at a specified time daily. Finally, it pushes a beautifully formatted "minimalist buy-side brief" directly to a Notion page.

## ✨ Core Features

* 🔄 **High-Frequency Automated Fetching**: Silent loop fetching based on customizable intervals (default is 30 minutes).


* 🎯 **Dynamic Configuration & Precise Matching**: Reads "Sectors" and "Keywords" (Chinese/English) in real-time from a Notion database to filter news.


* 🧠 **DeepSeek AI Brief Generation**: Integrates the DeepSeek model (`deepseek-v4-flash`) to generate a daily, no-nonsense "one-minute decision-making brief" highlighting core events and investment signals.


* 📓 **Seamless Notion Integration**: Both configuration reading and report publishing are handled within Notion. It natively supports parsing Markdown text into Notion rich-text blocks.


* 💾 **Local Data Persistence**: Built-in lightweight SQLite database (`rss_news.db`) with an anti-duplication mechanism to ensure data is not lost or pushed repeatedly.



## 🛠️ Prerequisites & Dependencies

Ensure you have **Python 3.8+** installed on your system.

After cloning the repository, install the required Python dependencies:

```bash
pip install requests feedparser openai python-dateutil python-dotenv

```

## ⚙️ Configuration Guide

The system relies on environment variables, local configuration files, and a specific Notion database structure. Please follow the steps below carefully.

### 1. Environment Variables (`.env`)

Create a `.env` file in the root directory of the project and fill in the following parameters:

```env
# Notion Credentials & Database IDs
NOTION_TOKEN=your_notion_integration_token
NOTION_DATABASE_ID=your_config_db_id
NOTION_REPORT_DB_ID=your_report_db_id

# RSSHub Base URL (Defaults to local port 1200, can be replaced with public/custom instances)
RSSHUB_BASE=http://127.0.0.1:1200

# DeepSeek API Key (Required for AI report generation)
DEEPSEEK_API_KEY=your_deepseek_api_key

```

### 2. RSS Feeds Configuration (`feeds.json`)

Create a `feeds.json` file in the root directory to configure the RSS feed routes you want to fetch (requires RSSHub):

```json
[
  {
    "name": "36Kr-Newsflash",
    "route": "/36kr/newsflashes"
  },
  {
    "name": "ITHome",
    "route": "/ithome/news"
  }
]

```

### 3. Notion Database Structure Requirements

The script uses the Notion API to read and write data. Please ensure your Notion databases contain the following specific property names:

**Config Database (Config DB)** - Corresponds to `NOTION_DATABASE_ID`:

* `Sector` (Title Property): The name of the sector, e.g., "Semiconductors", "AI Models".


* `CN Keywords` (Rich Text Property): Chinese keywords. Multiple keywords MUST be separated by a Chinese enumeration comma `、`.


* `EN Keywords` (Rich Text Property): English keywords. Multiple keywords MUST be separated by a Chinese enumeration comma `、`.



**Report Database (Report DB)** - Corresponds to `NOTION_REPORT_DB_ID`:

* Only requires a basic `Name` (Title) property. The script will automatically push the generated AI report as Page blocks.



## 🚀 How to Run

You can fine-tune the automation strategy by modifying the global variables at the top of `main.py`:

```python
# =================[Automation Strategy Config]=================
ENABLE_AI_REPORT = True         # Toggle: True to enable AI analysis and push, False to only fetch and save to DB
FETCH_INTERVAL_MINUTES = 30     # Fetch frequency: Allowed range is 10 ~ 1440 minutes
PUSH_TIME_UTC = "11:00"         # Daily report push time: UTC time (e.g., 11:00 UTC corresponds to 19:00 Beijing Time)
# ==============================================================

```

Once configured, run the main script:

```bash
python main.py

```

The terminal will output the startup logs, and the system will enter a continuous "perpetual motion" mode, sleeping and executing silently based on your set intervals.

## 📂 Project Structure

* `main.py`: The main program containing DB initialization, web scrapers, AI scheduling, and Notion integration.


* `feeds.json`: Local RSS feed configuration file.


* `.env`: Environment variables configuration file (Create manually, do not commit to version control).


* `rss_news.db`: SQLite local database file (Generated automatically upon the first run).



## ⚠️ Important Notes

* **Timezone Settings**: The AI data extraction logic evaluates the last 24 hours based on Beijing Time (UTC+8), while the push trigger (`PUSH_TIME_UTC`) strictly relies on UTC. Please calculate the difference accordingly.


* **RSSHub Dependency**: This project heavily relies on RSSHub to convert website news into standard RSS formats. It is recommended to deploy locally or use a stable RSSHub instance.
