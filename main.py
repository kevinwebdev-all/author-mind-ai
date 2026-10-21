
import logging
import sys

from telegram_bot import TelegramNovelBot


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        bot = TelegramNovelBot()
        bot.run()
    except KeyboardInterrupt:
        logging.info("AuthorMind AI stopped.")
    except Exception:
        logging.exception("AuthorMind AI failed to start.")
        sys.exit(1)


if __name__ == "__main__":
    main()
  
