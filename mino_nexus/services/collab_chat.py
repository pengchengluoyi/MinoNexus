"""Accept chat immediately; the existing durable queue owns planning and delegation."""
from mino_nexus.services.collab_conductor import enqueue


def chat(project_id, topic_id, sess, body, thread_id=''):
    return enqueue(project_id, topic_id, sess, body, thread_id)
