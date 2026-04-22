# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Flask-based web application for automated content review/auditing (内容审核). It's designed for reviewing marketing content from brand partnerships, using AI (Claude API) to check for violations against content standards.

**Key Features:**
- Web interface for content review workflow
- Integration with Feishu (Chinese collaboration platform) for spreadsheet operations
- AI-powered content analysis using Anthropic Claude API
- Support for both file upload (XLSX) and Feishu URL parsing
- Automated writing back of review results to source documents

## Architecture

**Backend:** Flask web server with RESTful API endpoints
**AI Integration:** Anthropic Claude API for content analysis
**External APIs:** Feishu API for document and spreadsheet operations
**File Processing:** Excel/CSV parsing with openpyxl, PDF reading with PyMuPDF
**Frontend:** Pure JavaScript with Server-Sent Events (SSE) for real-time progress

**Core Components:**
- `app.py` - Main Flask application with all routes and business logic
- `templates/index.html` - Single-page web interface
- `config.json` - Application configuration (API keys, endpoints)
- `uploads/` - Temporary storage for uploaded files
- `results/` - Generated review result files

## Development Commands

### Setup and Installation
```bash
# Install dependencies from requirements.txt
pip install -r requirements.txt

# Create configuration file from template
cp config.example.json config.json
# Edit config.json with your API keys
```

### Running the Application
```bash
# Run the development server
python app.py
# Starts on http://localhost:5000

# Run with debug mode (modify app.py to set debug=True)
python app.py
```

### Testing
```bash
# Run all tests
pytest test_app.py -v

# Run specific test class
pytest test_app.py::TestReviewOne -v

# Run with coverage
pytest test_app.py --cov=app
```

### Monitoring and Debugging
```bash
# Watch application logs in real-time
tail -f app.log

# Check log levels (configurable via LOG_LEVEL environment variable)
# Supported levels: DEBUG, INFO, WARNING, ERROR
```

## Configuration

### Option 1: Configuration File (config.json)
The application can be configured using `config.json`:

```json
{
  "api_key": "sk-...",           // Anthropic Claude API key
  "base_url": "https://...",     // Optional API base URL override
  "model": "claude-opus-4-6",    // Claude model to use
  "feishu_app_id": "cli_...",    // Feishu app ID for API access
  "feishu_app_secret": "..."     // Feishu app secret
}
```

### Option 2: Environment Variables
Alternatively, you can use environment variables:

```bash
export ANTHROPIC_API_KEY="sk-your-api-key"
export API_BASE_URL="https://ai-api.kkidc.com"  # Optional
export CLAUDE_MODEL="claude-opus-4-6"           # Optional
export FEISHU_APP_ID="cli_your-app-id"
export FEISHU_APP_SECRET="your-app-secret"
export DEFAULT_PDF_PATH="/path/to/rules.pdf"    # Optional
export LOG_LEVEL="INFO"                         # Optional: DEBUG,INFO,WARNING,ERROR
```

**Security Notes:** 
- The config.json file contains sensitive API keys and is excluded from version control (.gitignore)
- Environment variables are preferred for production deployments
- File upload size is limited to 10MB

## Key Workflows

### Content Review Process
1. User inputs Feishu spreadsheet URL or uploads Excel file
2. System parses document structure and identifies content rows
3. For each content item:
   - Fetches content from Feishu docs (if URL provided)
   - Applies category-specific and general review rules
   - Sends to Claude API for analysis
   - Receives structured response (passed/failed + violations)
4. Writes results back to source document
5. For failed reviews, adds comments to original Feishu documents

### Review Rules System
- Rules are fetched from Feishu spreadsheets or fallback PDF files
- Supports category-specific rules (e.g., "南京大牌档", "黑钻奔驰试驾")
- General platform rules apply to all content
- Rules are cached to avoid repeated API calls

## Important Functions

**Core Review Logic:**
- `review_one()` - Single content item AI review
- `_process_row()` - Full workflow for processing one spreadsheet row
- `get_rules()` - Fetches and caches review rules

**Feishu Integration:**
- `fetch_feishu_sheet()` - Parses Feishu spreadsheet data
- `fetch_feishu_content()` - Retrieves document content
- `write_feishu_sheet()` - Writes results back to spreadsheet
- `add_feishu_comment()` - Adds comments to documents

**File Processing:**
- `parse_xlsx()` - Processes uploaded Excel files
- `extract_hyperlinks()` - Extracts URLs from Excel hyperlinks

## API Endpoints

- `GET /` - Main web interface
- `GET|POST /api/config` - Configuration management
- `POST /api/parse-url` - Parse Feishu spreadsheet URL
- `POST /api/upload` - Upload Excel file for processing
- `GET /api/review/<task_id>` - Start review process (SSE stream)
- `GET /api/download/<task_id>` - Download processed results

## Data Flow

1. **Input Sources:** Feishu spreadsheets or uploaded Excel files
2. **Expected Columns:** 昵称 (username), 稿件链接 (content link), AI审核 (AI review status), 权益类型 (benefit type)
3. **Content Sources:** Feishu wiki/docx documents linked from spreadsheet cells
4. **Output:** Updated spreadsheet with review results, commented documents for violations

## Testing Strategy

The test suite covers:
- Pure functions (column conversion, text parsing, URL extraction)
- AI review logic with mocked API responses
- Flask route handlers with test client
- Error handling for external API failures
- End-to-end review workflow

Mock objects are used extensively for external dependencies (Anthropic API, Feishu API).

## Common Development Tasks

**Adding New Review Rules:**
Modify the `REVIEW_SYSTEM` constant or update the rules parsing logic in `get_rules()`.

**Supporting New File Formats:**
Extend the file upload handler and add parsing logic similar to `parse_xlsx()`.

**Modifying AI Review Logic:**
Update the prompt template in `review_one()` or adjust the response parsing logic.

**Adding New Feishu Features:**
Implement new API calls following the pattern in existing Feishu functions, with proper error handling.

## Directory Structure

```
├── app.py                 # Main Flask application with all business logic
├── test_app.py           # Comprehensive test suite  
├── templates/index.html  # Single-page web interface
├── uploads/              # Temporary file storage (auto-created, gitignored)
├── results/              # Generated result files (auto-created, gitignored)
├── config.json           # Runtime configuration (gitignored)
├── config.example.json   # Configuration template
├── requirements.txt      # Python dependencies
├── app.log               # Application logs (gitignored)
└── CLAUDE.md            # Development guidance (this file)
```

## Troubleshooting

### Common Issues

**API Connection Problems:**
- Verify API keys in config.json or environment variables
- Check network connectivity and proxy settings
- Review app.log for detailed error messages

**Feishu Integration Issues:**
- Confirm Feishu app has proper permissions (read/write spreadsheets, comment on docs)
- Verify spreadsheet sharing permissions allow app access
- Check Feishu app credentials are current

**File Processing Errors:**
- Ensure uploaded files are .xlsx format (not .xls)
- Verify file size is under 10MB limit
- Check column headers match expected Chinese names (昵称, 稿件链接, AI审核, 权益类型)