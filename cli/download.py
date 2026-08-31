import pandas as pd
import yt_dlp
import os
import re
import argparse
import sys
import threading
import time

class LoadingSpinner:
    """Simple loading spinner with three dots animation"""
    def __init__(self, message="Loading"):
        self.message = message
        self.running = False
        self.thread = None
        
    def _animate(self):
        dots = 0
        while self.running:
            spinner = "." * (dots % 4)  # 0, 1, 2, 3 dots
            print(f"\r{self.message}{spinner}   ", end="", flush=True)
            dots += 1
            time.sleep(0.5)
    
    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._animate)
        self.thread.daemon = True
        self.thread.start()
    
    def stop(self, ok=True):
        self.running = False
        if self.thread:
            self.thread.join()
        suffix = "✅" if ok else "❌"
        print(f"\r{self.message}... {suffix}", flush=True)

def get_output_dir(csv_file):
    """Get output directory name from CSV filename"""
    # Remove path and extension to get just the filename
    filename = os.path.basename(csv_file)
    name_without_ext = os.path.splitext(filename)[0]
    return name_without_ext

def sanitize_filename(filename):
    """Remove invalid characters from filename"""
    return re.sub(r'[<>:"/\\|?*]', '', filename)

def song_exists(output_dir, artist, track, ext):
    """Check if song already exists in the output directory"""
    filename = sanitize_filename(f"{artist} - {track}")
    path = os.path.join(output_dir, f"{filename}.{ext}")
    return os.path.exists(path)

def create_ydl_opts(output_dir, artist, track, album, youtube_url, use_flac):
    """Create yt-dlp options with custom metadata"""
    filename = sanitize_filename(f"{artist} - {track}")

    if use_flac:
        extract_pp = {
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'flac',
        }
    else:
        extract_pp = {
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }

    return {
        'format': 'bestaudio/best',
        'outtmpl': f'{output_dir}/{filename}.%(ext)s',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'postprocessors': [extract_pp, {
            'key': 'FFmpegMetadata',
            'add_metadata': True,
        }],
        'writethumbnail': False,
        'writeinfojson': False,
        'embedsubtitles': False,
        'writeautomaticsub': False,
        'postprocessor_args': {
            'FFmpegMetadata': [
                '-metadata', f'title={track}',
                '-metadata', f'artist={artist}',
                '-metadata', f'album={album}',
                '-metadata', f'comment=Original YouTube URL: {youtube_url}',
            ]
        }
    }

def download_song(output_dir, query, artist, track, album, use_flac):
    """Download song with custom metadata"""
    # First, get the YouTube URL
    with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            if info and 'entries' in info and info['entries']:
                youtube_url = info['entries'][0]['webpage_url']
            else:
                youtube_url = "Unknown"
        except:
            youtube_url = "Unknown"
    
    # Create custom options with metadata
    opts = create_ydl_opts(output_dir, artist, track, album, youtube_url, use_flac)
    
    # Start loading spinner
    spinner = LoadingSpinner(f"🎵 Downloading: {query}")
    spinner.start()
    
    try:
        # Download with custom metadata
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"ytsearch1:{query}"])
        spinner.stop(ok=True)
    except Exception as e:
        spinner.stop(ok=False)
        raise e

def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description='Download songs from YouTube using CSV playlist',
        epilog='Example: python download.py playlist.csv flac',
    )
    parser.add_argument('playlist_file', help='Path to the CSV playlist file')
    parser.add_argument(
        'format_flag',
        nargs='?',
        default=None,
        choices=['flac'],
        help='Optional: pass "flac" for lossless FLAC (best available source audio)',
    )
    args = parser.parse_args()

    use_flac = args.format_flag == 'flac'
    csv_file = args.playlist_file
    
    # Check if CSV file exists
    if not os.path.exists(csv_file):
        print(f"❌ Error: CSV file '{csv_file}' not found!")
        sys.exit(1)
    
    # Get output directory name from CSV filename
    output_dir = get_output_dir(csv_file)
    
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    print(f"📁 Output directory: {output_dir}")
    if use_flac:
        print("🎼 Format: FLAC (bestaudio → flac, lossless container)")
    else:
        print("🎼 Format: MP3 @ 192 kbps")
    
    # Load CSV
    try:
        df = pd.read_csv(csv_file)
    except Exception as e:
        print(f"❌ Error reading CSV file: {e}")
        sys.exit(1)
    
    # Adjust these to match your CSV column names
    track_col = "Track Name"
    artist_col = "Artist Name(s)"
    album_col = "Album Name"
    
    total_songs = len(df)
    downloaded_count = 0
    skipped_count = 0
    
    print(f"📊 Found {total_songs} songs in playlist")
    print("=" * 50)
    
    for _, row in df.iterrows():
        track = str(row[track_col]).strip()
        artist = str(row[artist_col]).strip()
        album = str(row[album_col]).strip()
        
        if not track or not artist:
            continue
        
        query = f"{artist} - {track}"
        
        ext = 'flac' if use_flac else 'mp3'
        # Check if song already exists
        if song_exists(output_dir, artist, track, ext):
            print(f"⏭️  Skipping (already exists): {query}")
            skipped_count += 1
            continue
        
        print(f"   Album: {album}")
        try:
            download_song(output_dir, query, artist, track, album, use_flac)
            downloaded_count += 1
        except Exception as e:
            print(f"❌ Error with {query}: {e}")
    
    print("=" * 50)
    print(f"📈 Summary:")
    print(f"   Total songs: {total_songs}")
    print(f"   Downloaded: {downloaded_count}")
    print(f"   Skipped (already exist): {skipped_count}")

if __name__ == "__main__":
    main()
