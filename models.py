from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    xp = db.Column(db.Integer, default=0)
    rank = db.Column(db.String(5), default='D')

    def calculate_rank(self):
        if self.xp >= 10000: return 'S+'
        elif self.xp >= 5000: return 'S'
        elif self.xp >= 2000: return 'A'
        elif self.xp >= 1000: return 'B'
        elif self.xp >= 500: return 'C'
        elif self.xp >= 100: return 'D'
        else: return 'F'

    def update_rank(self):
        new_rank = self.calculate_rank()
        if self.xp < 0:
            self.rank = 'F'
        else:
            self.rank = new_rank
        db.session.commit()


class Review(db.Model):
    __tablename__ = 'review'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    anime_id = db.Column(db.Integer, nullable=False)
    episode_id = db.Column(db.Integer, nullable=True)
    content = db.Column(db.Text, nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    likes = db.Column(db.Integer, default=0)
    dislikes = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    votes = db.relationship('Vote', backref='review', lazy=True, cascade='all, delete-orphan')
    author = db.relationship('User', backref='reviews', lazy=True)

    def get_xp_change(self):
        total = self.likes + self.dislikes
        if total == 0:
            return 0
        ratio = self.likes / total
        if ratio >= 0.9: return 20
        elif ratio >= 0.7: return 10
        elif ratio >= 0.5: return 5
        elif ratio >= 0.3: return -5
        elif ratio >= 0.1: return -15
        else: return -30


class Vote(db.Model):
    __tablename__ = 'vote'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    review_id = db.Column(db.Integer, db.ForeignKey('review.id'), nullable=False)
    vote_type = db.Column(db.String(10), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('user_id', 'review_id', name='unique_user_review_vote'),)


class Claim(db.Model):
    __tablename__ = 'claim'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    review_id = db.Column(db.Integer, db.ForeignKey('review.id'), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship('User', backref='claims')
    review = db.relationship('Review', backref='claims')
