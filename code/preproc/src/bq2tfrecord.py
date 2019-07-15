# -*- coding: utf-8 -*-

"""
Process Chigaco Taxi dataset from BigQuery to TFRecords using TensorFlow Transform
"""

import argparse
import pandas as pd
import numpy as np
import datetime
import os
import sys
import tensorflow as tf
import json
import apache_beam as beam
import tensorflow_transform as tft

from tensorflow_transform.beam import impl
from tensorflow_transform.tf_metadata import dataset_schema
from tensorflow_transform.tf_metadata import dataset_metadata
from tensorflow_transform.coders import example_proto_coder
from tensorflow_transform.tf_metadata import metadata_io
from tensorflow_transform.beam.tft_beam_io import transform_fn_io, beam_metadata_io


try:
    try:
        from apache_beam.options.pipeline_options import PipelineOptions
    except ImportError:
        from apache_beam.utils.pipeline_options import PipelineOptions
except ImportError:
    from apache_beam.utils.options import PipelineOptions

import logging
logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

COMMUNITY_AREA_QUERY = """
    SELECT DISTINCT pickup_community_area FROM `bigquery-public-data.chicago_taxi_trips.taxi_trips`
    WHERE pickup_community_area IS NOT NULL
"""


def query_trips(start_date, end_date):

    query_str = """
        SELECT
            pickup_community_area,
            EXTRACT(DATE from trip_start_timestamp) as date,
            EXTRACT(HOUR from trip_start_timestamp) as hour,
            COUNT(*) as n_trips
                FROM `bigquery-public-data.chicago_taxi_trips.taxi_trips`
                WHERE trip_start_timestamp >= '{start_date}'
                AND trip_start_timestamp <'{end_date}'
                AND pickup_community_area is NOT NULL
                AND trip_start_timestamp is NOT NULL
        GROUP BY date, hour, pickup_community_area
        ORDER BY date, hour, pickup_community_area ASC
    """.format(start_date=start_date, end_date=end_date)
    # LIMIT 10

    return query_str


def query_znorm_stats(start_date, end_date):
    query_str = """
        SELECT pickup_community_area, AVG(n_trips) mean, STDDEV(n_trips)+1 std FROM
            (SELECT
                pickup_community_area,
                EXTRACT(DATE from trip_start_timestamp) as date,
                EXTRACT(HOUR from trip_start_timestamp) as hour,
                COUNT(*) as n_trips
                FROM `bigquery-public-data.chicago_taxi_trips.taxi_trips`
                    WHERE trip_start_timestamp >= '{start_date}'
                    AND trip_start_timestamp < '{end_date}'
                    AND pickup_community_area IS NOT NULL
                GROUP BY date, hour, pickup_community_area
                ORDER BY date, hour ASC)
        GROUP BY pickup_community_area
        ORDER BY pickup_community_area ASC
    """.format(start_date=start_date, end_date=end_date)

    return query_str


class ParseZnormStatsRow(beam.DoFn):
    def process(self, element):
        yield {'pickup_community_area': str(element['pickup_community_area']),
               'mean': float(element['mean']),
               'std': float(element['std'])
               }


def read_znorm_stats_from_bq(pipeline, start_date, end_date):
    query_str = query_znorm_stats(start_date, end_date)
    raw_data = (pipeline |
                "Read znorm stats from BigQuery from {} to {}".format(start_date, end_date) >> beam.io.Read(
                    beam.io.BigQuerySource(query=query_str, use_standard_sql=True)) |
                "Parse stats from {} to {}".format(start_date, end_date) >> beam.ParDo(ParseZnormStatsRow()))

    return raw_data


class CombineZnormStats(beam.CombineFn):

    def __init__(self):
        super(CombineZnormStats, self).__init__()

    def create_accumulator(self):
        return {}

    def add_input(self, accumulator, element):
        # accumulator.append(str(element.get('pickup_community_area')))
        for k in element.keys():
            if k not in accumulator:
                accumulator[k] = []
            accumulator[k].append(element[k])

        return accumulator

    def merge_accumulators(self, accumulators):
        output = {}
        for acc in accumulators:
            for k in acc:
                if k not in output:
                    output[k] = []
                output[k].extend(acc[k])

        return output

    def extract_output(self, output):
        return output


class ParseRow(beam.DoFn):
    def process(self, element):
        yield {'pickup_community_area': str(element['pickup_community_area']),
               'date': element['date'],
               'hour': int(element['hour']),
               'n_trips': float(element['n_trips'])
               }


def read_data_from_bq(pipeline, start_date, end_date):

    query_str = query_trips(start_date, end_date)
    raw_data = (pipeline |
                "Read data from BigQuery from {} to {}".format(start_date, end_date) >> beam.io.Read(
                    beam.io.BigQuerySource(query=query_str, use_standard_sql=True)) |
                "Parse BigQuery Row from {} to {}".format(start_date, end_date) >> beam.ParDo(ParseRow()))

    return raw_data


class CombineCommunityArea(beam.CombineFn):

    def __init__(self):
        super(CombineCommunityArea, self).__init__()

    def create_accumulator(self):
        return []

    def add_input(self, accumulator, element):
        accumulator.append(str(element.get('pickup_community_area')))
        return accumulator

    def merge_accumulators(self, accumulators):
        output = []
        for a in accumulators:
            output.extend(a)
        return output

    def extract_output(self, output):
        return list(set(output))


class GroupItemsByDate(beam.CombineFn):

    def __init__(self, community_area_list, date_range):
        super(GroupItemsByDate, self).__init__()
        self.community_area_list = community_area_list
        self.start = date_range[0]
        self.end = date_range[1]
        self.delta = self.end - self.start

    def create_accumulator(self):
        return {}

    def add_input(self, accumulator, element):

        date = element['date']
        hour = element['hour']
        community_area = element['pickup_community_area']
        n_trips = element['n_trips']

        if date not in accumulator:
            accumulator[date] = {}
        if hour not in accumulator[date]:
            accumulator[date][hour] = {}
        if community_area not in accumulator[date][hour]:
            accumulator[date][hour][community_area] = 0

        accumulator[date][hour][community_area] += n_trips

        return accumulator

    def merge_accumulators(self, accumulators):
        output = {}
        for i in range(self.delta.days + 1):

            date_key = (self.start + datetime.timedelta(days=i)
                        ).strftime('%Y-%m-%d')

            output[date_key] = {}
            for hour in range(24):
                output[date_key][hour] = {}
                for ca in self.community_area_list:
                    output[date_key][hour][ca] = 0

        for a in accumulators:
            for date, date_dict in a.items():
                for hour, hour_dict in date_dict.items():
                    for ca in hour_dict:
                        output[date][hour][ca] += hour_dict[ca]
        return output

    def extract_output(self, output):

        flattened_dict = {
            'date': [],
            'hour': [],
            'community_area': [],
            'n_trips': []
        }

        for date, date_dict in output.items():
            for hour, hour_dict in date_dict.items():
                for ca in hour_dict:
                    flattened_dict['date'].append(date)
                    flattened_dict['hour'].append(hour)
                    flattened_dict['community_area'].append(ca)
                    flattened_dict['n_trips'].append(hour_dict[ca])

        return flattened_dict


class ExtractRawTimeseriesWindow(beam.DoFn):
    def __init__(self,  window_size):
        self.window_size = window_size

    def process(self, element):

        flattened_df = pd.DataFrame(element)
        flattened_df['date'] = pd.to_datetime(flattened_df['date'])
        flattened_df['hour'] = pd.to_numeric(flattened_df['hour'])
        flattened_df['day_of_month'] = flattened_df['date'].apply(
            lambda t: t.day)
        flattened_df['day_of_week'] = flattened_df['date'].apply(
            lambda t: t.dayofweek)
        flattened_df['month'] = flattened_df['date'].apply(lambda t: t.month)
        flattened_df['week_number'] = flattened_df['date'].apply(
            lambda t: t.weekofyear)

        for ca, trips_time_series in flattened_df.groupby('community_area'):

            # force sorting
            ts_df = trips_time_series.sort_values(
                ['date', 'hour'], ascending=True)

            for i in range(0, (len(ts_df)-self.window_size-1), 1):
                window = ts_df.iloc[i:(i+self.window_size+1)]

                window_dict = {
                    'hour': None,
                    'day_of_week': None,
                    'day_of_month': None,
                    'week_number': None,
                    'month': None,
                    'n_trips': None,
                }

                for k in window_dict:
                    window_dict[k] = window[k].values[:self.window_size]

                window_dict['community_area'] = [int(ca)] * self.window_size
                window_dict['community_area_code'] = int(ca)

                # Add target
                window_dict['target'] = window['n_trips'].values[self.window_size].astype(
                    np.float32)

                yield window_dict


def scale_temporal_feature(value, period):
    scaled_value = tf.divide(tf.cast(value, tf.float32), period)

    return scaled_value


def process_temporal_features_sin(value, period):
    scaled_value = scale_temporal_feature(value, period)
    value_sin = tf.math.sin(2*np.pi*scaled_value)

    return value_sin


def process_temporal_features_cos(value, period):
    scaled_value = scale_temporal_feature(value, period)
    value_cos = tf.math.cos(2*np.pi*scaled_value)

    return value_cos


def preprocess_fn(features, window_size, znorm_stats):

    output_features = {}

    output_features['hour_sin'] = process_temporal_features_sin(
        features['hour'], 25)
    output_features['hour_cos'] = process_temporal_features_cos(
        features['hour'], 25)
    output_features['day_of_week_sin'] = process_temporal_features_sin(
        features['day_of_week'], 8)
    output_features['day_of_week_cos'] = process_temporal_features_cos(
        features['day_of_week'], 8)
    output_features['day_of_month_sin'] = process_temporal_features_sin(
        features['day_of_month'], 32)
    output_features['day_of_month_cos'] = process_temporal_features_cos(
        features['day_of_month'], 32)
    output_features['week_number_sin'] = process_temporal_features_sin(
        features['week_number'], 55)
    output_features['week_number_cos'] = process_temporal_features_cos(
        features['week_number'], 55)
    output_features['month_sin'] = process_temporal_features_sin(
        features['month'], 13)
    output_features['month_cos'] = process_temporal_features_cos(
        features['month'], 13)

    output_features['community_area'] = features['community_area']
    # output_features['community_area_code'] = features['community_area_code']

    # convert znorm statistics into tensor lookup table
    # (TENSORFLOW 1.14)
    # lookup_mean = tf.lookup.StaticHashTable(
    #     tf.lookup.KeyValueTensorInitializer(keys=[np.int64(int(i)) for i in znorm_stats['pickup_community_area']],
    #                                         values=znorm_stats['mean'],
    #                                         key_dtype=tf.int64,
    #                                         value_dtype=tf.float32),
    #     default_value=0)

    # lookup_std = tf.lookup.StaticHashTable(
    #     tf.lookup.KeyValueTensorInitializer(keys=[np.int64(int(i)) for i in znorm_stats['pickup_community_area']],
    #                                         values=znorm_stats['std'],
    #                                         key_dtype=tf.int64,
    #                                         value_dtype=tf.float32),
    #     default_value=1)

    # (TENSORFLOW 1.13.1)
    lookup_mean = tf.contrib.lookup.HashTable(
        tf.contrib.lookup.KeyValueTensorInitializer(keys=[np.int64(int(i)) for i in znorm_stats['pickup_community_area']],
                                            values=znorm_stats['mean'],
                                            key_dtype=tf.int64,
                                            value_dtype=tf.float32),
        default_value=0)

    lookup_std = tf.contrib.lookup.HashTable(
        tf.contrib.lookup.KeyValueTensorInitializer(keys=[np.int64(int(i)) for i in znorm_stats['pickup_community_area']],
                                            values=znorm_stats['std'],
                                            key_dtype=tf.int64,
                                            value_dtype=tf.float32),
        default_value=1)


    znorm_tensor_mean = lookup_mean.lookup(
        keys=features['community_area_code'])
    znorm_tensor_std = lookup_std.lookup(keys=features['community_area_code'])

    # force shape
    znorm_tensor_mean = tf.reshape(znorm_tensor_mean, [-1, 1])
    znorm_tensor_std = tf.reshape(znorm_tensor_std, [-1, 1])
    target = tf.reshape(features['target'], [-1, 1])

    # normalize
    output_features['n_trips'] = tf.math.divide(
        tf.math.subtract(features['n_trips'], znorm_tensor_mean), znorm_tensor_std)
    output_features['target'] = tf.math.divide(
        tf.math.subtract(target, znorm_tensor_mean), znorm_tensor_std)

    # reshape
    for k in ['hour_sin', 'hour_cos', 'day_of_week_sin',
              'day_of_week_cos', 'day_of_month_sin', 'day_of_month_cos',
              'week_number_sin', 'week_number_cos',
              'month_sin', 'month_cos', 'n_trips']:

        output_features[k] = tf.reshape(
            output_features[k], [-1, window_size, 1])

    return output_features


def get_feature_spec(window_size):

    schema_dict = {
        'hour': tf.FixedLenFeature(shape=[window_size], dtype=tf.int64, default_value=None),
        'day_of_week': tf.FixedLenFeature(shape=[window_size], dtype=tf.int64, default_value=None),
        'day_of_month': tf.FixedLenFeature(shape=[window_size], dtype=tf.int64, default_value=None),
        'week_number': tf.FixedLenFeature(shape=[window_size], dtype=tf.int64, default_value=None),
        'month': tf.FixedLenFeature(shape=[window_size], dtype=tf.int64, default_value=None),
        'community_area': tf.FixedLenFeature(shape=[window_size], dtype=tf.int64, default_value=None),
        'n_trips': tf.FixedLenFeature(shape=[window_size], dtype=tf.float32, default_value=None),
        'community_area_code': tf.FixedLenFeature(shape=[], dtype=tf.int64, default_value=None),
        'target': tf.FixedLenFeature(shape=[], dtype=tf.float32, default_value=None)
    }

    schema = dataset_metadata.DatasetMetadata(
        dataset_schema.from_feature_spec(schema_dict))
    return schema


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--tfrecord-dir', dest='tfrecord_dir', required=True)
    parser.add_argument('--tfx-artifacts-dir',
                        dest='tfx_artifacts_dir', required=True)
    parser.add_argument('--project', dest='project', required=True)
    parser.add_argument('--window-size', dest='window_size',
                        type=int, required=False, default=24)
    parser.add_argument('--start-date', dest='start_date', required=True)
    parser.add_argument('--end-date', dest='end_date', required=True)
    parser.add_argument('--split-date', dest='split_date', required=True)
    parser.add_argument('--temp-dir', dest='temp_dir',
                        required=False, default='/tmp')
    known_args, pipeline_args = parser.parse_known_args()

    pipeline_args.append('--project')
    pipeline_args.append(known_args.project)

    start_datetime = datetime.datetime.strptime(
        known_args.start_date, '%Y-%m-%d')
    end_datetime = datetime.datetime.strptime(known_args.end_date, '%Y-%m-%d')
    split_datetime = datetime.datetime.strptime(
        known_args.split_date, '%Y-%m-%d')

    pipeline_options = PipelineOptions(flags=pipeline_args)

    # Query metadata
    with beam.Pipeline(options=pipeline_options) as pipeline:

        # List eligible community areas
        community_area_list_p = (pipeline |
                                 'Query eligible Community Areas' >> beam.io.Read(beam.io.BigQuerySource(
                                     query=COMMUNITY_AREA_QUERY, use_standard_sql=True)) |
                                 'Combine list' >> beam.CombineGlobally(CombineCommunityArea()) |
                                 'Map to string' >> beam.Map(lambda l: ','.join(l)) |
                                 'Dump to text' >> beam.io.WriteToText(os.path.join(
                                     known_args.tfx_artifacts_dir, 'community_area_list.json'), shard_name_template='')
                                 )

        # Query znorm statistics
        znorm_stats_p = read_znorm_stats_from_bq(
            pipeline, known_args.start_date, known_args.split_date)

        _ = (znorm_stats_p |
             "Combine znorm Stats" >> beam.CombineGlobally(CombineZnormStats()) |
             "Map znorm stats to json" >> beam.Map(lambda d: json.dumps(d)) |
             "Dump znorm stats to file" >> beam.io.WriteToText(os.path.join(
                 known_args.tfx_artifacts_dir, 'znorm_stats.json'), shard_name_template='')
             )

    community_area_list = open(os.path.join(known_args.tfx_artifacts_dir,
                                            'community_area_list.json')).read().strip().split(',')

    znorm_stats = json.load(open(os.path.join(known_args.tfx_artifacts_dir,
                                              'znorm_stats.json')))

    # Add some epsilon to avoid division by zero
    # znorm_stats['std'] = [i+1 for i in znorm_stats['std']]

    # Preprocess dataset
    with beam.Pipeline(options=pipeline_options) as pipeline:
        with impl.Context(known_args.temp_dir):

            # Process training data
            raw_data_train = read_data_from_bq(
                pipeline, known_args.start_date, known_args.split_date)

            orders_by_date_train = (raw_data_train |
                                    "Merge - train" >> beam.CombineGlobally(GroupItemsByDate(community_area_list, (start_datetime, split_datetime))))

            ts_windows_train = (orders_by_date_train | "Extract timeseries windows - train" >>
                                beam.ParDo(ExtractRawTimeseriesWindow(known_args.window_size)))

            # _ = ts_windows_train | "print ts_windows_train" >> beam.Map(print)

            ts_windows_schema = get_feature_spec(known_args.window_size)
            norm_ts_windows_train, transform_fn = ((ts_windows_train, ts_windows_schema) |
                                                   "Analyze and Transform - train" >> impl.AnalyzeAndTransformDataset(lambda t: preprocess_fn(t,
                                                                                                                                              known_args.window_size,
                                                                                                                                              znorm_stats)))
            norm_ts_windows_train_data, norm_ts_windows_train_metadata = norm_ts_windows_train

            _ = norm_ts_windows_train_data | 'Write TFrecords - train' >> beam.io.tfrecordio.WriteToTFRecord(
                file_path_prefix=os.path.join(
                    known_args.tfrecord_dir, 'train'),
                file_name_suffix=".tfrecords",
                coder=example_proto_coder.ExampleProtoCoder(norm_ts_windows_train_metadata.schema))

            # Process evaluation data
            raw_data_eval = read_data_from_bq(
                pipeline, known_args.split_date,  known_args.end_date)

            orders_by_date_eval = (raw_data_eval |
                                   "Merge SKUs - eval" >> beam.CombineGlobally(GroupItemsByDate(community_area_list, (split_datetime, end_datetime))))

            ts_windows_eval = (orders_by_date_eval | "Extract timeseries windows - eval" >>
                               beam.ParDo(ExtractRawTimeseriesWindow(known_args.window_size)))

            norm_ts_windows_eval = (((ts_windows_eval, ts_windows_schema), transform_fn) |
                                    "Transform - eval" >> impl.TransformDataset())
            norm_ts_windows_eval_data, norm_ts_windows_eval_metadata = norm_ts_windows_eval

            _ = norm_ts_windows_eval_data | 'Write TFrecords - eval' >> beam.io.tfrecordio.WriteToTFRecord(
                file_path_prefix=os.path.join(known_args.tfrecord_dir, 'eval'),
                file_name_suffix=".tfrecords",
                coder=example_proto_coder.ExampleProtoCoder(norm_ts_windows_eval_metadata.schema))

            # Dump transformation graph
            _ = transform_fn | 'Dump Transform Function Graph' >> transform_fn_io.WriteTransformFn(
                known_args.tfx_artifacts_dir)            
