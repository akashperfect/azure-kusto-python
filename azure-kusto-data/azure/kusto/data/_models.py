"""Kusto Data Models"""

import json
from datetime import datetime, timedelta
from enum import Enum
from decimal import Decimal
import six
from . import _converters
from .exceptions import KustoServiceError


keep_high_precision_values = True

try:
    import pandas
except:
    keep_high_precision_values = False


def _get_precise_repr(t, raw_value, typed_value, **kwargs):
    if t == "datetime":
        lookback = kwargs.get("lookback")
        seventh_char = kwargs.get("seventh_char")
        last = kwargs.get("last")

        if seventh_char and seventh_char.isdigit():
            return raw_value[:-lookback] + seventh_char + "00" + last

        return raw_value
    elif t == "timespan":
        seconds_fractions_part = kwargs.get("seconds_fractions_part")
        if seconds_fractions_part:
            whole_part = int(typed_value.total_seconds())
            fractions = str(whole_part) + "." + seconds_fractions_part
            total_seconds = float(fractions)

            return total_seconds

        return typed_value.total_seconds()
    else:
        raise ValueError("Unknown type {t}".format(t))


class WellKnownDataSet(Enum):
    """Categorizes data tables according to the role they play in the data set that a Kusto query returns."""

    PrimaryResult = "PrimaryResult"
    QueryCompletionInformation = "QueryCompletionInformation"
    TableOfContents = "TableOfContents"
    QueryProperties = "QueryProperties"


class KustoResultRow(object):
    """Iterator over a Kusto result row."""

    convertion_funcs = {"datetime": _converters.to_datetime, "timespan": _converters.to_timedelta, "decimal": Decimal}

    def __init__(self, columns, row):
        self._value_by_name = {}
        self._value_by_index = []
        self._hidden_values = []
        self._seventh_digit = {}
        for i, value in enumerate(row):
            column = columns[i]
            try:
                column_type = column.column_type.lower()
            except AttributeError:
                self._value_by_index.append(value)
                self._value_by_name[columns[i]] = value
                if keep_high_precision_values:
                    self._hidden_values.append(value)
                continue

            if column_type in ["datetime", "timespan"]:
                if value is None:
                    typed_value = None
                    if keep_high_precision_values:
                        self._hidden_values.append(None)

                else:
                    seconds_fractions_part = None
                    seventh_char = None
                    last = value[-1] if isinstance(value, six.string_types) and value[-1].isalpha() else ""
                    lookback = None

                    try:
                        # If you are here to read this, you probably hit some datetime/timedelta inconsistencies.
                        # Azure-Data-Explorer(Kusto) supports 7 decimal digits, while the corresponding python types supports only 6.
                        # What we do here, is remove the 7th digit, if exists, and create a datetime/timedelta
                        # from whats left. The reason we are keeping the 7th digit, is to allow users to work with
                        # this precision in case they want it. One example why one might want this precision, is when
                        # working with pandas. In that case, use azure.kusto.data.helpers.dataframe_from_result_table
                        # which takes into account the 7th digit.
                        seconds_part = value.split(":")[2]
                        seconds_fractions_part = seconds_part.split(".")[1]
                        seventh_char = seconds_fractions_part[6]

                        if seventh_char.isdigit():
                            tick = int(seventh_char)
                            lookback = 2 if last else 1                            
                            typed_value = KustoResultRow.convertion_funcs[column_type](value[:-lookback] + last)

                            if tick:
                                if column_type == "datetime":
                                    self._seventh_digit[column.column_name] = tick
                                elif column_type == "timespan":
                                    self._seventh_digit[column.column_name] = (
                                        tick if abs(typed_value) == typed_value else -tick
                                    )
                                else:
                                    raise TypeError("Unexpected type {}".format(column_type))
                        else:
                            typed_value = KustoResultRow.convertion_funcs[column_type](value)
                    except (IndexError, AttributeError):
                        typed_value = KustoResultRow.convertion_funcs[column_type](value)

                    # this is a special case where plain python will lose precision, so we keep the precise value hidden
                    # when transforming to pandas, we can use the hidden value to convert to precise pandas/numpy types
                    if keep_high_precision_values:
                        self._hidden_values.append(
                            _get_precise_repr(
                                column_type,
                                value,
                                typed_value,
                                seconds_fractions_part=seconds_fractions_part,
                                last=last,
                                lookback=lookback,
                                seventh_char=seventh_char,
                            )
                        )
            elif column_type in KustoResultRow.convertion_funcs:
                typed_value = KustoResultRow.convertion_funcs[column_type](value)
                if keep_high_precision_values:
                    self._hidden_values.append(value)
            else:
                typed_value = value
                if keep_high_precision_values:
                    self._hidden_values.append(value)

            self._value_by_index.append(typed_value)
            self._value_by_name[column.column_name] = typed_value

    @property
    def columns_count(self):
        return len(self._value_by_name)

    def __iter__(self):
        for i in range(self.columns_count):
            yield self[i]

    def __getitem__(self, key):
        if isinstance(key, six.integer_types):
            return self._value_by_index[key]
        return self._value_by_name[key]

    def __len__(self):
        return self.columns_count

    def to_dict(self):
        return self._value_by_name

    def to_list(self):
        return self._value_by_index

    def __str__(self):
        return "['{}']".format("', '".join([str(val) for val in self._value_by_index]))

    def __repr__(self):
        values = [repr(val) for val in self._value_by_name.values()]
        return "KustoResultRow(['{}'], [{}])".format("', '".join(self._value_by_name), ", ".join(values))


class KustoResultColumn(object):
    def __init__(self, json_column, ordianl):
        self.column_name = json_column["ColumnName"]
        self.column_type = json_column.get("ColumnType") or json_column["DataType"]
        self.ordinal = ordianl

    def __repr__(self):
        return "KustoResultColumn({},{})".format(
            json.dumps({"ColumnName": self.column_name, "ColumnType": self.column_type}), self.ordinal
        )


class KustoResultTable(object):
    """Iterator over a Kusto result table."""

    def __init__(self, json_table):
        self.table_name = json_table.get("TableName")
        self.table_id = json_table.get("TableId")
        self.table_kind = WellKnownDataSet[json_table["TableKind"]] if "TableKind" in json_table else None
        self.columns = [KustoResultColumn(column, index) for index, column in enumerate(json_table["Columns"])]

        errors = [row for row in json_table["Rows"] if isinstance(row, dict)]
        if errors:
            raise KustoServiceError(errors[0]["OneApiErrors"][0]["error"]["@message"], json_table)

        self.rows = [KustoResultRow(self.columns, row) for row in json_table["Rows"]]

    @property
    def _rows(self):
        for row in self.rows:
            yield row._hidden_values

    @property
    def rows_count(self):
        return len(self.rows)

    @property
    def columns_count(self):
        return len(self.columns)

    def to_dict(self):
        """Converts the table to a dict."""
        return {"name": self.table_name, "kind": self.table_kind, "data": [r.to_dict() for r in self]}

    def __len__(self):
        return self.rows_count

    def __iter__(self):
        for row in self.rows:
            yield row

    def __getitem__(self, key):
        return self.rows[key]

    def __str__(self):
        d = self.to_dict()
        # enum is not serializable, using value instead
        d["kind"] = d["kind"].value
        return json.dumps(d)

    def __bool__(self):
        return any(self.columns)

    __nonzero__ = __bool__
