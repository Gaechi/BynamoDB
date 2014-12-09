from boto.dynamodb2.layer1 import DynamoDBConnection
from boto.dynamodb2.fields import HashKey, RangeKey
from boto.dynamodb.types import get_dynamodb_type
from boto.dynamodb2.types import Dynamizer

from .attributes import Attribute
from .exceptions import NullAttributeException
from .indexes import Index, GlobalIndex
from .results import Result


class ModelMeta(type):
    """Model meta class"""
    def __new__(mcs, clsname, bases, dct):
        for name, val in dct.items():
            if isinstance(val, Attribute):
                val.attr_name = name
            elif type(val) == type and issubclass(val, Index):
                val._keys = [HashKey(val.hash_key, dct[val.hash_key].type)]
                if val.range_key:
                    val._keys.append(RangeKey(val.range_key,
                                              dct[val.range_key].type))
        return super(ModelMeta, mcs).__new__(mcs, clsname, bases, dct)


class Model(object):
    """Defines schema of a DynamoDB table with
    :class:`~bynamodb.attributes.Attribute` and
    :class:`~bynamodb.attributes.indexes.Index`.
    You can execute APIs of the table through the model.

    """
    __metaclass__ = ModelMeta

    #: (:class:`str`) The table name.
    #: # If omitted, the Model class name will be the table name.
    table_name = None

    _attributes = None
    _conn = None
    _keys = None
    _indexes = None

    def __init__(self, data):
        """An object of the Model represents an item of the model.

        :param data: key value of the item.
        :type data: :class:`collections.Mapping`

        """
        self._data = {}
        self._set_defaults()
        for name, value in data.items():
            if name in dir(self):
                setattr(self, name, value)
            else:
                continue

    def _set_defaults(self):
        for attr in self._get_attributes().values():
            if attr.default:
                value = attr.default
                if callable(value):
                    value = value()
                setattr(self, attr.attr_name, value)

    def save(self):
        self._put_item(self)

    @classmethod
    def create_table(cls, read_throughput=5, write_throughput=5):
        """Create the table as the schema definition."""
        table_name = cls.get_table_name()

        raw_throughput = {
            'ReadCapacityUnits': read_throughput,
            'WriteCapacityUnits': write_throughput
        }

        table_schema = []
        table_definitions = []
        seen_attrs = set()
        for key in cls._get_keys():
            table_schema.append(key.schema())
            table_definitions.append(key.definition())
            seen_attrs.add(key.name)

        indexes = []
        global_indexes = []
        for index in cls._get_indexes():
            if issubclass(index, GlobalIndex):
                global_indexes.append(index.schema())
            else:
                indexes.append(index.schema())
            for key in index._keys:
                if key.name not in seen_attrs:
                    table_definitions.append(key.definition())
                    seen_attrs.add(key.name)

        cls._get_connection().create_table(
            table_name=table_name,
            key_schema=table_schema,
            attribute_definitions=table_definitions,
            provisioned_throughput=raw_throughput,
            local_secondary_indexes=indexes or None,
            global_secondary_indexes=global_indexes or None
        )

    @classmethod
    def query(cls, key_filter, filter_builder=None, **kwargs):
        """High level query API.

        :param key_filter: key conditions of the query.
        :type key_filter: :class:`collections.Mapping`
        :param filter_builder: filter expression builder.
        :type filter_builder: :class:`~bynamodb.filterexps.Operator`
        """
        kwargs['key_conditions'] = cls._build_filter(key_filter)
        if 'query_filter' in kwargs:
            kwargs['query_filter'] = cls._build_filter(kwargs['query_filter'])
        if filter_builder:
            cls._build_filter_expression(filter_builder, kwargs)
        result = cls._get_connection().query(cls.get_table_name(), **kwargs)
        return Result(cls, result)

    @classmethod
    def _build_filter(cls, key_filter):
        filters = {}
        dynamizer = Dynamizer()
        for field_and_op, value in key_filter.items():
            try:
                field, op = field_and_op.split('__')
            except Exception:
                raise ValueError('key filter expression is not valid')
            # TODO: Implement multiple value encoding
            filters[field] = {
                'ComparisonOperator': op.upper(),
                'AttributeValueList': [dynamizer.encode(value)]
            }
        return filters

    @classmethod
    def scan(cls, filter_builder=None, **kwargs):
        """High level scan API.

        :param filter_builder: filter expression builder.
        :type filter_builder: :class:`~bynamodb.filterexps.Operator`

        """
        if filter_builder:
            cls._build_filter_expression(filter_builder, kwargs)
        result = cls._get_connection().scan(cls.get_table_name(), **kwargs)
        return Result(cls, result)

    @classmethod
    def _build_filter_expression(cls, filter_builder, kwargs):
        kwargs['filter_expression'], kwargs['expression_attribute_values'] = \
            filter_builder.build_exp()

    @classmethod
    def put_item(cls, data, **kwargs):
        """Put item to the table.

        :param data: key value of the item.
        :type data: :class:`collections.Mapping`

        """
        cls._put_item(cls(data), **kwargs)

    @classmethod
    def _put_item(cls, item, **kwargs):
        data = {}
        for name, attr in cls._get_attributes().items():
            attr_value = getattr(item, name, None)
            if attr_value is None:
                if not attr.null:
                    raise NullAttributeException(
                        'Attribute {0} cannot be null'.format(name))
                else:
                    continue
            attr_value_type = get_dynamodb_type(attr_value)
            if attr_value_type != attr.type:
                raise ValueError(
                    'Expected type for {0}: {1}, actual type: {2}'.format(
                        name, attr, attr_value_type
                    ))
            data[attr.attr_name] = attr_value
        cls._get_connection().put_item(cls.get_table_name(), data, **kwargs)

    @classmethod
    def get_item(cls, hash_key=None, range_key=None, **kwargs):
        """ Get item from the table."""
        if hash_key:
            kwargs['key'] = cls._encode_key(hash_key, range_key)
        raw_data = cls._get_connection().get_item(cls.get_table_name(),
                                                  **kwargs)
        return cls.deserialize(raw_data['Item'])

    @classmethod
    def deserialize(cls, item_raw):
        """Translate the raw item data from the DynamoDBConnection
        to the item object.

        """
        dynamizer = Dynamizer()
        deserialized = {}
        for name, attr in item_raw.items():
            deserialized[name] = dynamizer.decode(attr)
        return cls(deserialized)

    @classmethod
    def _encode_key(cls, hash_key, range_key=None):
        dynamizer = Dynamizer()
        encoded = {cls._get_hash_key().name: dynamizer.encode(hash_key)}
        if range_key:
            encoded.update(
                {cls._get_range_key().name: dynamizer.encode(range_key)})
        return encoded

    @classmethod
    def get_table_name(cls):
        return cls.table_name or cls.__name__

    @classmethod
    def _get_keys(cls):
        if cls._keys:
            return cls._keys
        hash_key = None
        range_key = None
        for attr in cls._get_attributes().values():
            if attr.hash_key:
                hash_key = HashKey(attr.attr_name, attr.type)
            elif attr.range_key:
                range_key = RangeKey(attr.attr_name, attr.type)
        cls._keys = [key for key in [hash_key, range_key] if key]
        return cls._keys

    @classmethod
    def _get_hash_key(cls):
        return cls._get_keys()[0]

    @classmethod
    def _get_range_key(cls):
        return cls._get_keys()[1]

    @classmethod
    def _get_attributes(cls):
        if cls._attributes:
            return cls._attributes
        cls._attributes = {}
        for item_name in dir(cls):
            item_cls = getattr(getattr(cls, item_name), '__class__', None)
            if item_cls is not None and issubclass(item_cls, Attribute):
                cls._attributes[item_name] = getattr(cls, item_name)
        return cls._attributes

    @classmethod
    def _get_indexes(cls):
        if cls._indexes:
            return cls._indexes
        cls._indexes = []
        for item_name in dir(cls):
            attr = getattr(cls, item_name)
            if type(attr) == type and issubclass(attr, Index):
                cls._indexes.append(attr)
        return cls._indexes

    @classmethod
    def _get_connection(cls):
        if cls._conn:
            return cls._conn
        cls._conn = DynamoDBConnection()
        return cls._conn
