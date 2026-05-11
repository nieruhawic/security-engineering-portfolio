# GoogleDriveToS3Sync

A desktop application that downloads files from a Google Drive folder and uploads them to an AWS S3 bucket. It handles ZIP files, Google-native documents, and regular files, and produces a CSV report of everything that was uploaded.

---

## Features

- **Google Drive integration** — Authenticates via OAuth and browses a specified Drive folder.
- **ZIP support** — Automatically downloads and extracts ZIP files before uploading their contents to S3.
- **Google Docs export** — Converts Google-native files to portable formats automatically:
  - Google Docs → PDF
  - Google Sheets → XLSX
  - Google Slides → PPTX
  - Google Drawings → PNG
- **Direct file support** — Non-ZIP, non-Google files (PDFs, images, etc.) are downloaded and uploaded as-is.
- **Drive subfolder support** — Files inside Drive subfolders are downloaded and their folder structure is preserved in S3.
- **Safe S3 key naming** — Whitespace in file and folder names is replaced with underscores. Duplicate filenames get a `__DUP1`, `__DUP2` suffix to prevent collisions.
- **Skip existing** — Optionally skip files that already exist in S3 to avoid redundant uploads.
- **Dry run mode** — Preview what would be uploaded without actually writing anything to S3.
- **CSV report** — After each run a CSV is saved with one row per file, including S3 URI, file size, SHA-256 hash, folder path, and upload status.
- **Progress tracking** — A dual progress bar shows overall run progress and per-file stage progress.
- **Live log** — A scrollable log tab shows every action and any errors in real time.

---

## Requirements

- Windows 10 or 11
- Python 3.10+
- A Google Cloud project with the Drive API enabled and an OAuth 2.0 client secret JSON file
- AWS credentials with `s3:PutObject` (and `s3:HeadObject` if using skip-existing)

Install Python dependencies:

```bash
pip install -r requirements.txt
```

---

## Google OAuth Setup

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project (or use an existing one).
3. Enable the **Google Drive API**.
4. Go to **APIs & Services → Credentials** and create an **OAuth 2.0 Client ID** (Desktop app).
5. Download the client secret JSON file.
6. In the app, point the **Client Secret File** field to that JSON file.

On first run the app will open a browser window for you to sign in to Google and grant Drive read access. A token is saved to your home directory so you only need to do this once.

---

## AWS Credentials

Enter your credentials directly in the app's Config tab:

| Field | Description |
|---|---|
| Access Key ID | Your AWS access key |
| Secret Access Key | Your AWS secret key |
| Session Token | Optional — required for temporary/STS credentials |
| Region | AWS region of your S3 bucket (e.g. `us-east-1`) |

Use the **Test AWS Connection** button to verify credentials before running.

---

## Usage

Run the app:

```bash
python main.py
```

Then in the **Config** tab:

1. Sign in to Google with the **Sign in** button.
2. Paste your **Google Drive Folder ID** (the long ID from the folder's URL).
3. Set your local **ZIP download directory** and **unzip directory**.
4. Set the **CSV output path** for the upload report.
5. Fill in your **S3 bucket**, optional **S3 prefix**, and AWS credentials.
6. Click **Test AWS Connection** to confirm access.
7. Choose whether to enable **Skip Existing** and/or **Dry Run**.
8. Click **Start** to begin.

Switch to the **Log** tab at any time to see detailed progress. Click **Stop** to cancel a run in progress.

---

## CSV Report

After each run a CSV is saved with the following columns:

| Column | Description |
|---|---|
| Timestamp | When the run was executed |
| ZipName | Name of the ZIP the file came from (if applicable) |
| Folder1–Folder10 | Folder path segments (up to 10 levels deep) |
| FileName | File name |
| RelativePathOriginal | Original relative path before sanitization |
| RelativePathS3 | Sanitized path used as the S3 key suffix |
| SanitizedSpaces | YES if any spaces were replaced with underscores |
| SizeBytes | File size in bytes |
| Sha256 | SHA-256 hash of the file |
| S3Bucket | Target S3 bucket |
| S3Key | Full S3 object key |
| S3Uri | Full S3 URI (`s3://bucket/key`) |
| UploadStatus | `UPLOADED`, `SKIPPED_EXISTS`, `SKIPPED`, `DRYRUN`, or `ERROR` |
| Error | Error message if the upload failed |

---

## Building a Standalone Executable

PyInstaller is included in the requirements. To build a single `.exe`:

```bash
pyinstaller --onefile --windowed main.py
```

The executable will be in the `dist/` folder.

---

## Screenshots

_Add screenshots here_

---

## License

MIT License — free to use, modify, and distribute.
