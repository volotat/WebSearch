from datetime import datetime
from src.db_models import db


class WebPage(db.Model):
    id = db.Column(db.Integer, unique=True, primary_key=True)
    hash = db.Column(db.String, nullable=True, unique=True)
    hash_algorithm = db.Column(db.String, nullable=True, default=None)
    file_path = db.Column(db.String, nullable=True)
    url = db.Column(db.String, nullable=True, index=True)       # kept for fast recrawl matching
    raw_hash = db.Column(db.String, nullable=True)              # blake2b of raw HTTP bytes, for fast recrawl change detection
    user_rating = db.Column(db.Float, nullable=True)
    user_rating_date = db.Column(db.DateTime, nullable=True)
    model_rating = db.Column(db.Float, nullable=True)
    model_hash = db.Column(db.String, nullable=True)
    last_viewed = db.Column(db.DateTime, nullable=True, default=None)

    def as_dict(self):
        return {column.name: getattr(self, column.name) for column in self.__table__.columns}
