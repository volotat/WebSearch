from datetime import datetime
from src.db_models import db


class WebPage(db.Model):
    """Represents a single crawled web page stored as a .md file."""
    id = db.Column(db.Integer, primary_key=True)
    hash = db.Column(db.String, nullable=True, unique=True)
    hash_algorithm = db.Column(db.String, nullable=True, default=None)
    url = db.Column(db.String, nullable=False, unique=True)
    domain = db.Column(db.String, nullable=False, index=True)  # e.g. "example.com"
    url_path = db.Column(db.String, nullable=True)             # path portion of the URL
    md_file_path = db.Column(db.String, nullable=True)         # relative path to the .md file inside storage dir
    title = db.Column(db.String, nullable=True)
    preview_text = db.Column(db.String, nullable=True)         # first ~300 chars of the .md content
    user_rating = db.Column(db.Float, nullable=True)
    user_rating_date = db.Column(db.DateTime, nullable=True)
    model_rating = db.Column(db.Float, nullable=True)
    model_hash = db.Column(db.String, nullable=True)
    crawl_date = db.Column(db.DateTime, nullable=True)
    last_crawl_date = db.Column(db.DateTime, nullable=True)

    def as_dict(self):
        d = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        # Serialize datetimes for JSON transport
        for key in ('crawl_date', 'last_crawl_date', 'user_rating_date'):
            if key in d and d[key] is not None:
                d[key] = d[key].isoformat()
        return d
