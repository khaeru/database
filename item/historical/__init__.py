from copy import copy
import os

import pandas as pd
import pint
import yaml

from item.common import paths
from item.remote import OpenKAPSARC, get_sdmx


# List of data processing scripts/IPython notebooks
SCRIPTS = [
    'T000',
    'T001',
    'T002',
    'T003',
    'T004',
    'T005',
    'T006',
    'T007',
    'T008'
]


# Define a registry for tracking of units, and add units appearing in the data.
ureg = pint.UnitRegistry()
ureg.define("""
idx_2005_100 = [index, 2005=100]
person = [person]
passenger = person
TEU = [container]
TOE = 41.9 GJ
vehicle = [vehicle]
yr = year
""".strip())


def convert_units(row, target_units=[]):
    """Convert a DataFrame *row* to *target_units*.

    The 'Value' and 'Unit' columns of *row* are converted into the first
    compatible (i.e. same dimensionality) units from *target_units*.

    *target_units* contains zero or more (scaling_factor, target_unit) tuples,
    where *target_unit* is a pint.Unit and scaling_factor is a float.
    """
    result = ureg.Quantity(row['Value'], row['Unit'])
    scaling_factor = 1

    # If no target_units are supplied, this code does not run
    for scaling_factor, target_unit in target_units:
        try:
            result = result.to(target_unit)
            break
        except pint.DimensionalityError:
            # Not a compatible unit for this row
            continue
        except Exception as e:
            print(e, result, target_unit)
            raise

    # Set the resulting values
    row['Value'] = result.magnitude
    row['Unit'] = result.units

    # scaling_factor had the value from the preferred unit
    if scaling_factor != 1:
        row['Value'] /= scaling_factor
        row['Unit'] *= scaling_factor

    return row


def source_str(id):
    """Return the canonical string name (e.g. 'T001') for a data source."""
    return f'T{id:03}' if isinstance(id, int) else id


def fetch_source(id, use_cache=True):
    """Fetch data from source *id*.

    Parameters
    ----------
    use_cache : bool, optional
        If given, use a cached file. No check of cache validity is performed.
    """
    # Retrieve source information from sources.yaml
    id = source_str(id)
    source_info = copy(SOURCES[id])

    # Path for cached data. NB OpenKAPSARC does its own caching
    cache_path = paths['historical input'] / f'{id}.csv'

    if use_cache and cache_path.exists():
        return cache_path

    # Information for fetching the data
    fetch_info = source_info['fetch']

    remote_type = fetch_info.pop('type')
    if remote_type.lower() == 'sdmx':
        # Use SDMX to retrieve the data
        result = get_sdmx(**fetch_info)
    elif remote_type.lower() == 'openkapsarc':
        # Retrieve data using the OpenKAPSARC API
        ok_api = OpenKAPSARC(api_key=os.environ.get('OK_API_KEY', None))
        result = ok_api.table(**fetch_info)
    else:
        raise ValueError(remote_type)

    # Cache the results
    result.to_csv(cache_path, index=False)

    return cache_path


def input_file(id: int):
    """Return the path to a cached, raw input data file for data source *id*.

    CSV files are located in the 'historical input' data path. If more than
    one file has a name beginning with “T{id}”, the last sorted file is
    returned.
    """
    # List of all matching files
    all_files = sorted(paths['historical input']
                       .glob(f'{source_str(id)}*.csv'))

    # The last file has the most recent timestamp
    return all_files[-1]


def map_values(row, mapping):
    """Return a mapped value from a DataFrame *row*.

    *mapping* is a dict with a key '_column'. The value in this column of *row*
    is used to return a value from *mapping*.
    """
    column = mapping['_column']
    return mapping[row[column]]


def preferred_units(info):
    """Return a list of (scaling_factor, unit) tuples.

    *info['preferred_units']* may contain a list of one or more strings
    describing units with optional scaling factors.
    """
    result = []

    for unit in info.get('preferred_units', []):
        try:
            result.append[(1, ureg.parse_units(unit))]
        except ValueError:
            # *unit* has a scaling factor, e.g. 10³ km. Split this to the
            # scaling factor and the pure unit (e.g. 'km').
            qty = ureg.parse_expression(unit)
            result.append[(qty.magnitude, qty.units)]
        except Exception as e:
            print(e, unit)
            raise

    return result


# Function of conversion phase 1
def conversion_layer1(df, top_dict={}):
    """Convert *df* to a standard format.

    *top_dict* may contain each of the following keys, which are processed in
    the order that they appear here. See mapping_conv_phase1.yaml for concrete
    examples.

    - 'rename': a mapping from original column names to final column names.
      All other columns are dropped.
    - 'add': a mapping from column names to values which are used to fill all
      rows. The value may itself be a mapping; in this case, it is passed to
      :meth:`map_values` to look up the values in the 'add' column from values
      in the '_column' column.
    - 'replace': a mapping from column names to a replacement mapping that is
      applied to that column.
    - 'preferred_units': a list of target units

    Parameters
    ----------
    df : pd.DataFrame
        Raw data set loaded from IK2 Open Data.
    top_dict : dict, optional
        Dictionary of mapping rules.
    """

    # Rename existing columns to preferred dimensions, dropping others
    rename = top_dict.get('rename', {})
    df = df.take(rename.keys(), axis=1) \
           .rename(columns=rename)

    # Add columns that are not included and mapping with pre-defined rules
    for col, value in top_dict.get('add', {}).items():
        if '_depend' in value:
            df[col] = df.apply(map_values, axis=1, args=(value,))
        else:
            df[col] = value

    # Replace values in fields
    for col, mapping in top_dict.get('replace', {}).items():
        df[col].replace(mapping, inplace=True)

    # Replace '-' with ' '. pint interprets '-' to mean subtraction.
    df['Unit'] = df['Unit'].str.replace('-', ' ')

    # Convert units
    df = df.apply(convert_units, axis=1, args=(preferred_units(top_dict),))

    return df


with open(paths['data'] / 'historical' / 'sources.yaml') as f:
    SOURCES = yaml.safe_load(f)


def main(output_file, use_cache):
    """Convert the input datasets.

    A single dataframe with the output is written to *output_file*.
    If *use_cache* is True, local files are used before querying the API.
    """
    # Get the configuration
    # TODO load these from all the files appearing in one directory, so that
    #      each data set can be specified in a separate file
    ds_info_path = paths['data'] / 'historical' / 'mapping_conv_phase1.yaml'
    all_ds_info = yaml.safe_load(open(ds_info_path))

    # Access the OpenKAPSARC API
    ok = OpenKAPSARC()

    list_of_df = []

    for ds_info in all_ds_info:
        if use_cache:
            # Locate a previously-cached CSV file
            cache_path = (paths['historical'] /
                          ds_info['uid']).with_suffix('.csv')
            try:
                df = pd.read_csv(cache_path, sep=';')
                cache_miss = False
            except FileNotFoundError:
                cache_miss = True

        if not use_cache or cache_miss:
            # Retrieve the data from the OpenKAPSARC datahub
            df = ok.table(ds_info['id'])

        # Process the data
        df = conversion_layer1(df, ds_info)

        # Add a source annotation
        df['Source'] = f"OpenKAPSARC:{ds_info['uid']}"

        # Store
        list_of_df.append(df)

    # Combine to single dataframe
    df_output = pd.concat(list_of_df, sort=False, ignore_index=True)
    df_output = df_output[["Region", "Variable", "Unit", "Mode", "Technology",
                           "Fuel", "Year", "Value", "Source"]]

    df_output.to_csv(output_file, index=False)

    return df_output
