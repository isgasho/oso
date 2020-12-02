"""SQLAlchemy hooks that transparently enable oso on SQLAlchemy operations.

There are several potential interfaces to integrate oso with SQLAlchemy:

    - :py:func:`authorized_sessionmaker`: (**recommended**) Session factory that
       creates a session that applies authorization on every query.
    - :py:func:`enable_hooks`: Globally enable oso on all queries.
    - :py:func:`make_authorized_query_cls`: Make a query class that is
       authorized before execution.

.. note::

    If using any API besides :py:func:`authorized_sessionmaker`, ensure you set
    ``enable_baked_queries=False`` on the session. Query caching can interfere
    with authorization.

    It is recommended to scope authorization context (the oso instance, user and
    action) to a single session.  Otherwise, the identity map (SQLAlchemy's
    cache of retrieved objects) may contain objects that were authorized for a
    previous user. This could cause incorrect behavior.

    :py:func:`authorized_sessionmaker` will enforce this.  If the authorization
    context changes during the session, an Exception will be raised.
"""
import functools
from typing import Any, Callable

from sqlalchemy.event import listen, remove
from sqlalchemy import event
from sqlalchemy.orm.query import Query
from sqlalchemy.orm import aliased, sessionmaker, Session
from sqlalchemy import orm

from oso import Oso

from sqlalchemy_oso.auth import authorize_model_filter


@event.listens_for(Query, "before_compile", retval=True)
def before_compile(query):
    """Enable before compile hook."""
    return _authorize_query(query)


def _authorize_query(query: Query) -> Query:
    """Authorize an existing query with an oso instance, user and action."""
    # Get the query session.
    session = query.session

    # Check whether this is an oso session.
    if not isinstance(session, AuthorizedSessionBase):
        # Not an authorized session.
        return None

    oso = session.oso_context['oso']
    user = session.oso_context['user']
    action = session.oso_context['action']

    # TODO (dhatch): This is necessary to allow ``authorize_query`` to work
    # on queries that have already been made.  If a query has a LIMIT or OFFSET
    # applied, SQLAlchemy will by default throw an error if filters are applied.
    # This prevents these errors from occuring, but could result in some
    # incorrect queries. We should remove this if possible.
    query = query.enable_assertions(False)

    entities = {column["entity"] for column in query.column_descriptions}
    for entity in entities:
        # Only apply authorization to columns that represent a mapper entity.
        if entity is None:
            continue

        authorized_filter = authorize_model_filter(
            oso, user, action, query.session, entity
        )
        if authorized_filter is not None:
            query = query.filter(authorized_filter)

    return query


def authorized_sessionmaker(get_oso, get_user, get_action, class_=None, **kwargs):
    """Session factory for sessions with oso authorization applied.

    :param get_oso: Callable that return oso instance to use for authorization.
    :param get_user: Callable that returns user for an authorization request.
    :param get_action: Callable that returns action for the authorization request.

    All other positional and keyword arguments are passed through to
    :py:func:`sqlalchemy.orm.session.sessionmaker` unchanged.
    """
    if class_ is None:
        class_ = Session

    # oso, user and action must remain unchanged for the entire session.
    # If they change before a query runs, an error is thrown.
    # This is to prevent objects that are unauthorized from ending up in the
    # session's identity map.
    class Sess(AuthorizedSessionBase, class_):
        def __init__(self, **options):
            options.setdefault("oso", get_oso())
            options.setdefault("user", get_user())
            options.setdefault("action", get_action())
            super().__init__(**options)

    session = type("Session", (Sess,), {})

    # We call sessionmaker here because sessionmaker adds a configure
    # method to the returned session and we want to replicate that
    # functionality.
    return sessionmaker(class_=session, **kwargs)


def scoped_session(get_oso, get_user, get_action, scopefunc=None, **kwargs):
    """Return a scoped session maker that uses the user and action as part of the scope function.

    Uses authorized_sessionmaker as the factory.

    :param scopefunc: Additional scope function to use for scoping sessions.
                      Output will be combined with the oso, action and user objects.
    :param kwargs: Additional keyword arguments to pass to
                   authorized_sessionmaker.
    """
    scopefunc = scopefunc or (lambda: None)

    def _scopefunc():
        return (get_oso(), get_action(), get_user(), scopefunc())

    factory = authorized_sessionmaker(get_oso, get_user, get_action, **kwargs)

    return orm.scoped_session(factory, scopefunc=_scopefunc)


class AuthorizedSessionBase(object):
    """Session that uses oso authorization for queries."""

    def __init__(self, oso: Oso, user, action, **options):
        """Create an authorized session using ``oso``.

        :param user: The user to perform authorization for.
        :param action: The action to authorize.
        :param options: Additional keyword arguments to pass to ``Session``.

        The user and action parameters are fixed for a given session. This
        prevents authorization responses from changing, ensuring that the
        identity map never contains unauthorized objects.
        """
        self._oso = oso
        self._oso_user = user
        self._oso_action = action

        super().__init__(**options)

    @property
    def oso_context(self):
        return {
            'oso': self._oso,
            'user': self._oso_user,
            'action': self._oso_action
        }

class AuthorizedSession(AuthorizedSessionBase, Session):
    pass
