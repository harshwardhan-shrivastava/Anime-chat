from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash
from flask_login import login_required, current_user
from models import db, Review, Vote, User, Claim
from datetime import datetime

reviews_bp = Blueprint('reviews', __name__)

@reviews_bp.route('/reviews')
def reviews_page():
    reviews = Review.query.order_by(Review.created_at.desc()).all()
    return render_template('reviews.html', reviews=reviews)

@reviews_bp.route('/rate_anime/<int:anime_id>', methods=['GET', 'POST'])
@login_required
def rate_anime(anime_id):
    if request.method == 'POST':
        rating = request.form.get('rating', type=int)
        content = request.form.get('content')
        episode_id = request.form.get('episode_id')
        if episode_id:
            episode_id = int(episode_id)
        else:
            episode_id = None

        existing = Review.query.filter_by(
            user_id=current_user.id,
            anime_id=anime_id,
            episode_id=episode_id
        ).first()
        if existing:
            flash('You already reviewed this!', 'warning')
            return redirect(url_for('reviews.rate_anime', anime_id=anime_id))

        review = Review(
            user_id=current_user.id,
            anime_id=anime_id,
            episode_id=episode_id,
            content=content,
            rating=rating
        )
        db.session.add(review)
        current_user.xp += 5
        current_user.update_rank()
        db.session.commit()
        flash('Review submitted!', 'success')
        return redirect(url_for('reviews.reviews_page'))

    return render_template('rate_anime.html', anime_id=anime_id)

@reviews_bp.route('/api/review/<int:review_id>/vote', methods=['POST'])
@login_required
def vote_review(review_id):
    data = request.get_json()
    vote_type = data.get('vote_type')
    review = Review.query.get_or_404(review_id)

    existing = Vote.query.filter_by(user_id=current_user.id, review_id=review_id).first()
    if existing:
        if existing.vote_type == vote_type:
            return jsonify({'error': 'Already voted'}), 400
        if existing.vote_type == 'like':
            review.likes -= 1
        else:
            review.dislikes -= 1
        existing.vote_type = vote_type
    else:
        new_vote = Vote(user_id=current_user.id, review_id=review_id, vote_type=vote_type)
        db.session.add(new_vote)

    if vote_type == 'like':
        review.likes += 1
    else:
        review.dislikes += 1
    db.session.commit()

    xp_change = review.get_xp_change()
    current_user.xp += xp_change
    current_user.update_rank()
    db.session.commit()

    return jsonify({
        'success': True,
        'likes': review.likes,
        'dislikes': review.dislikes,
        'xp_change': xp_change,
        'new_xp': current_user.xp,
        'new_rank': current_user.rank
    })

@reviews_bp.route('/reviews/claim/<int:review_id>', methods=['POST'])
@login_required
def claim_review(review_id):
    reason = request.form.get('reason')
    if not reason:
        flash('Reason required', 'danger')
        return redirect(url_for('reviews.reviews_page'))

    existing = Claim.query.filter_by(user_id=current_user.id, review_id=review_id, status='pending').first()
    if existing:
        flash('Already have a pending claim', 'warning')
        return redirect(url_for('reviews.reviews_page'))

    claim = Claim(user_id=current_user.id, review_id=review_id, reason=reason)
    db.session.add(claim)
    db.session.commit()
    flash('Claim submitted!', 'success')
    return redirect(url_for('reviews.reviews_page'))

@reviews_bp.route('/admin/claims')
@login_required
def admin_claims():
    if current_user.username != 'harshwardhan-shrivastava':
        flash('Unauthorized', 'danger')
        return redirect(url_for('index'))
    claims = Claim.query.filter_by(status='pending').all()
    return render_template('admin_claims.html', claims=claims)

@reviews_bp.route('/admin/claim/<int:claim_id>/action', methods=['POST'])
@login_required
def admin_claim_action(claim_id):
    if current_user.username != 'harshwardhan-shrivastava':
        return jsonify({'error': 'Unauthorized'}), 403
    claim = Claim.query.get_or_404(claim_id)
    action = request.form.get('action')
    if action == 'approve':
        claim.status = 'approved'
        review = Review.query.get(claim.review_id)
        if review:
            user = User.query.get(review.user_id)
            user.xp += 15
            user.update_rank()
    else:
        claim.status = 'rejected'
    db.session.commit()
    flash(f'Claim {action}ed', 'success')
    return redirect(url_for('reviews.admin_claims'))
