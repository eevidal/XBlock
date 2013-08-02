"""
Machinery to make the common case easy when building new runtimes
"""

import functools
import itertools
import re
from abc import ABCMeta, abstractmethod
from collections import namedtuple, MutableMapping
from xml.etree import ElementTree as ET

from .core import ModelType, BlockScope, Scope, XBlock


class InvalidScopeError(Exception):
    """
    Raised to indicated that operating on the supplied scope isn't allowed by a KeyValueStore
    """
    pass


class NoSuchViewError(Exception):
    """
    Raised to indicate that the view requested was not found.
    """
    pass


class NoSuchHandlerError(Exception):
    """
    Raised to indicate that the requested handler was not found.
    """
    pass


class KeyValueStore(object):
    """The abstract interface for Key Value Stores."""

    # Keys are structured to retain information about the scope of the data.
    # Stores can use this information however they like to store and retrieve
    # data.
    Key = namedtuple("Key", "scope, student_id, block_scope_id, field_name")

    def get(self, key):
        """Abstract get method. Implementations should return the value of the given `key`."""
        pass

    def set(self, key, value):
        """Abstract set method. Implementations should set `key` equal to `value`."""
        pass

    def delete(self, key):
        """Abstract delete method. Implementations should remove the `key`."""
        pass

    def has(self, key):
        """Abstract has method. Implementations should return Boolean, whether or not `key` is present."""
        pass

    def set_many(self, update_dict):
        """Abstract set_many method. Implementations should accept an `update_dict` of
        key-value pairs, and set all the `keys` to the given `value`s."""
        pass

class BlockId(namedtuple('BlockId', 'usage_id def_id')):
    pass


class MemoryKeyValueStore(KeyValueStore):
    """Use a simple in-memory database for a key-value store."""

    def __init__(self, d=None):
        self.d = d or {}
        self._ids = itertools.count()

    def clear(self):
        """Clear all data from the store."""
        self.d.clear()

    def new_block_id(self):
        return BlockId(str(next(self._ids)), str(next(self._ids)))

    def actual_key(self, key):
        k = []
        if key.scope == Scope.children:
            k.append('children')
        elif key.scope == Scope.parent:
            k.append('parent')
        else:
            k.append(["usage", "definition", "type", "all"][key.scope.block])

        if key.block_scope_id is not None:
            k.append(key.block_scope_id)
        if key.student_id:
            k.append(key.student_id)
        return ".".join(k)

    def get(self, key):
        return self.d[self.actual_key(key)][key.field_name]

    def set(self, key, value):
        """Sets the key to the new value"""
        self.d.setdefault(self.actual_key(key), {})[key.field_name] = value

    def delete(self, key):
        del self.d[self.actual_key(key)][key.field_name]

    def has(self, key):
        return key.field_name in self.d[self.actual_key(key)]

    def as_html(self):
        """Just for our Workbench!"""
        html = json.dumps(self.d, sort_keys=True, indent=4)
        return make_safe_for_html(html)

    def set_many(self, update_dict):
        """
        Sets many fields to new values in one call.

        `update_dict`: A dictionary of keys: values.
        This method sets the value of each key to the specified new value.
        """
        for key, value in update_dict.items():
            # We just call `set` directly here, because this is an in-memory 
            # representation thus we don't concern ourselves with bulk writes.
            self.set(key, value)




class DbModel(MutableMapping):
    """A dictionary-like interface to the fields on a block."""

    def __init__(self, kvs, block_cls, student_id, block_id):
        self._kvs = kvs
        self._student_id = student_id
        self._block_cls = block_cls
        self._block_id = block_id

    def __repr__(self):
        return "<{0.__class__.__name__} {0._block_cls!r}>".format(self)

    def _getfield(self, name):
        """
        Return the field with the given `name`.
        If no field with `name` exists in any namespace, raises a KeyError.
        """

        # First, get the field from the class, if defined
        block_field = getattr(self._block_cls, name, None)
        if block_field is not None and isinstance(block_field, ModelType):
            return block_field

        # If the class doesn't have the field, and it also doesn't have any
        # namespaces, then the name isn't a field so KeyError
        if not hasattr(self._block_cls, 'namespaces'):
            raise KeyError(name)

        # Resolve the field name in the first namespace where it's available.
        for namespace_name in self._block_cls.namespaces:
            namespace = getattr(self._block_cls, namespace_name)
            namespace_field = getattr(type(namespace), name, None)
            if namespace_field is not None and isinstance(namespace_field, ModelType):
                return namespace_field

        # Not in the class or in any of the namespaces, so name
        # really doesn't name a field
        raise KeyError(name)

    def _key(self, name):
        """
        Resolves `name` to a key, in the following form:

        KeyValueStore.Key(
            scope=field.scope,
            student_id=student_id,
            block_scope_id=block_id,
            field_name=name
        )
        """
        field = self._getfield(name)
        if field.scope in (Scope.children, Scope.parent):
            block_id = self._block_id.usage_id
            student_id = None
        else:
            block = field.scope.block

            if block == BlockScope.ALL:
                block_id = None
            elif block == BlockScope.USAGE:
                block_id = self._block_id.usage_id
            elif block == BlockScope.DEFINITION:
                block_id = self._block_id.def_id
            elif block == BlockScope.TYPE:
                block_id = self._block_cls.__name__

            if field.scope.user:
                student_id = self._student_id
            else:
                student_id = None

        key = KeyValueStore.Key(
            scope=field.scope,
            student_id=student_id,
            block_scope_id=block_id,
            field_name=name
        )
        return key

    def __getitem__(self, name):
        return self._kvs.get(self._key(name))

    def __setitem__(self, name, value):
        self._kvs.set(self._key(name), value)

    def __delitem__(self, name):
        self._kvs.delete(self._key(name))

    def __iter__(self):
        return iter(self.keys())

    def __len__(self):
        return len(self.keys())

    def __contains__(self, name):
        try:
            return self._kvs.has(self._key(name))
        except KeyError:
            return False

    def keys(self):
        fields = [field.name for field in self._block_cls.fields]
        for namespace_name in self._block_cls.namespaces:
            fields.extend(field.name for field in getattr(self._block_cls, namespace_name).fields)
        return fields

    def update(self, other_dict=None, **kwargs):
        """Update the underlying model with the correct values."""
        updated_dict = {}
        other_dict = other_dict or {}
        # Combine all the arguments into a single dict.
        other_dict.update(kwargs)

        # Generate a new dict with the correct mappings.
        for (key, value) in other_dict.items():
            updated_dict[self._key(key)] = value

        self._kvs.set_many(updated_dict)



class Runtime(object):
    """
    Access to the runtime environment for XBlocks.

    A pre-configured instance of this class will be available to XBlocks as
    `self.runtime`.

    """
    def __init__(self):
        self._view_name = None

    def render(self, block, context, view_name):
        """
        Render a block by invoking its view.

        Finds the view named `view_name` on `block`.  The default view will be
        used if a specific view hasn't be registered.  If there is no default
        view, an exception will be raised.

        The view is invoked, passing it `context`.  The value returned by the
        view is returned, with possible modifications by the runtime to
        integrate it into a larger whole.

        """
        self._view_name = view_name

        view_fn = getattr(block, view_name, None)
        if view_fn is None:
            view_fn = getattr(block, "fallback_view", None)
            if view_fn is None:
                raise NoSuchViewError()
            view_fn = functools.partial(view_fn, view_name)

        frag = view_fn(context)

        # Explicitly save because render action may have changed state
        block.save()
        self._view_name = None
        return self.wrap_child(block, frag, context)

    def get_block(self, block_id):
        """Get a block by ID.

        Returns the block identified by `block_id`, or raises an exception.

        """
        raise NotImplementedError("Runtime needs to provide get_block()")

    def render_child(self, child, context, view_name=None):
        """A shortcut to render a child block.

        Use this method to render your children from your own view function.

        If `view_name` is not provided, it will default to the view name you're
        being rendered with.

        Returns the same value as :func:`render`.

        """
        return child.runtime.render(child, context, view_name or self._view_name)

    def render_children(self, block, context, view_name=None):
        """Render a block's children, returning a list of results.

        Each child of `block` will be rendered, just as :func:`render_child` does.

        Returns a list of values, each as provided by :func:`render`.

        """
        results = []
        for child_id in block.children:
            child = self.get_block(child_id)
            result = self.render_child(child, context, view_name)
            results.append(result)
        return results

    def wrap_child(self, _block, frag, _context):
        """
        Wraps the fragment with any necessary HTML, informed by
        the block and the context. This default implementation
        simply returns the fragment.
        """
        # By default, just return the fragment itself.
        return frag

    def handle(self, block, handler_name, data):
        """
        Handles any calls to the specified `handler_name`.

        Provides a fallback handler if the specified handler isn't found.
        """
        handler = getattr(block, handler_name, None)
        if handler:
            # Cache results of the handler call for later saving
            results = handler(data)
        else:
            fallback_handler = getattr(block, "fallback_handler", None)
            if fallback_handler:
                # Cache results of the handler call for later saving
                results = fallback_handler(handler_name, data)
            else:
                raise NoSuchHandlerError("Couldn't find handler %r for %r" % (handler_name, block))

        # Write out dirty fields
        block.save()
        return results

    def handler_url(self, url):
        """Get the actual URL to invoke a handler.

        `url` is the abstract URL to your handler.  It should start with the
        name you used to register your handler.

        The return value is a complete absolute URL that will route through the
        runtime to your handler.

        """
        raise NotImplementedError("Runtime needs to provide handler_url()")

    def query(self, block):
        """Query for data in the tree, starting from `block`.

        Returns a Query object with methods for navigating the tree and
        retrieving information.

        """
        raise NotImplementedError("Runtime needs to provide query()")

    def querypath(self, block, path):
        """An XPath-like interface to `query`."""
        class BadPath(Exception):
            """Bad path exception thrown when path cannot be found."""
            pass
        # pylint: disable=C0103
        q = self.query(block)
        ROOT, SEP, WORD, FINAL = range(4)
        state = ROOT
        lexer = RegexLexer(
            ("dotdot", r"\.\."),
            ("dot", r"\."),
            ("slashslash", r"//"),
            ("slash", r"/"),
            ("atword", r"@\w+"),
            ("word", r"\w+"),
            ("err", r"."),
        )
        for tokname, toktext in lexer.lex(path):
            if state == FINAL:
                # Shouldn't be any tokens after a last token.
                raise BadPath()
            if tokname == "dotdot":
                # .. (parent)
                if state == WORD:
                    raise BadPath()
                q = q.parent()
                state = WORD
            elif tokname == "dot":
                # . (current node)
                if state == WORD:
                    raise BadPath()
                state = WORD
            elif tokname == "slashslash":
                # // (descendants)
                if state == SEP:
                    raise BadPath()
                if state == ROOT:
                    raise NotImplementedError()
                q = q.descendants()
                state = SEP
            elif tokname == "slash":
                # / (here)
                if state == SEP:
                    raise BadPath()
                if state == ROOT:
                    raise NotImplementedError()
                state = SEP
            elif tokname == "atword":
                # @xxx (attribute access)
                if state != SEP:
                    raise BadPath()
                q = q.attr(toktext[1:])
                state = FINAL
            elif tokname == "word":
                # xxx (tag selection)
                if state != SEP:
                    raise BadPath()
                q = q.children().tagged(toktext)
                state = WORD
            else:
                raise BadPath("Invalid thing: %r" % toktext)
        return q


    def register_child(self, child_node):
        raise NotImplementedError("Runtime needs to provide register_child()")


class RuntimeSystem(object):
    """
    A RuntimeSystem is a self contained piece that holds within it some
    number of XBlocks and their state management.

    It knows:
    * Who the user is
    * What the KVStore is

    It's responsible for:
    * Creating new XBlocks with the appropriate Runtimes and DbModels
    * Holding a tree of XBlocks in its own pocket universe (if desired -- this
      just means we're shoving it into its own KVStore
    * Maintaining parent/child relationships?
    * Maintaining local block_ids?
    """

    def __init__(self, kv_store=None, student_id=None):
        self._root_block = None
        self._kv_store = kv_store or MemoryKeyValueStore()
        self._student_id = student_id

    def create_block(self, tag_name, block_id=None):
        """
        Given an XML `tag_name`, create a new XBlock. The `RuntimeSystem` gets
        to decide what `XBlock` will be instantiated (possibly based off of
        system configuration or user preferences). The `RuntimeSystem` is also
        responsible for provisioning the appropriate DbModel and Runtime for a
        given XBlock.

        This method should instantiate an XBlock and return it, but should not
        do any other initialization (call other methods) on the XBlock.
        """
        block_cls = self._block_class_for_tag(tag_name)

        block_id = block_id or self._kv_store.new_block_id()

        runtime = self._provision_runtime(block_cls, block_id)
        model = self._provision_model(block_cls, block_id)
        block = block_cls(runtime, model, block_id)

        return block

    def copy(self, kv_store):
        """Copy the XBlocks contained in this RuntimeSystem to a new
        KeyValueStore."""
        pass

    def load_xml(self, xml):
        root_node = self._parse_xml(xml)
        block = self.create_block(root_node.tag)
        block.load_xml(root_node, create_block_func=self.create_block)

        self._root_block = block

        return block

    def dump_xml(self):
        return self.root_block.dump_xml()

    @property
    def root_block(self):
        return self._root_block


    # Non-public methods that can be overridden to tweak basic behavior
    def _block_class_for_tag(self, tag_name):
        """Given a `tag_name`, return the XBlock class that should handle it.
        Currently just falls back on `XBlock.load_class`.
        """
        return XBlock.load_class(tag_name)

    def _provision_runtime(self, block_cls, block_id):
        return Runtime()

    def _provision_model(self, block_cls, block_id):
        return DbModel(self._kv_store, block_cls, self._student_id, block_id)


    # Small helper methods
    def _parse_xml(self, xml):
        return ET.fromstring(xml) if isinstance(xml, basestring) else xml




class RegexLexer(object):
    """Split text into lexical tokens based on regexes."""
    def __init__(self, *toks):
        parts = []
        for name, regex in toks:
            parts.append("(?P<%s>%s)" % (name, regex))
        self.regex = re.compile("|".join(parts))

    def lex(self, text):
        """Iterator that tokenizes `text` and yields up tokens as they are found"""
        for match in self.regex.finditer(text):
            name = match.lastgroup
            yield (name, match.group(name))
