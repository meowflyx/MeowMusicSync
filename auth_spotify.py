from config import SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SPOTIPY_REDIRECT_URI, validate_spotify_config
from error_messages import explain_error
import sys
from spotipy.oauth2 import SpotifyOAuth
import spotipy

def main():
    validate_spotify_config(SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SPOTIPY_REDIRECT_URI)
    print("Инициализация Spotify Auth...")
    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope="user-library-read user-library-modify",
        open_browser=False
    )
    sp = spotipy.Spotify(auth_manager=auth_manager)
    
    # Делаем тестовый запрос, чтобы триггернуть авторизацию
    user = sp.current_user()
    print(f"\n✅ Успешно авторизовано как: {user.get('display_name') or user.get('id') or user.get('account_id') or 'пользователь Spotify'}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("Авторизация отменена.")
    except Exception as error:
        sys.exit(explain_error(error))
