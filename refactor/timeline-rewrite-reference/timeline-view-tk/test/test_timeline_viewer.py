"""Timeline viewer unit tests.

Copyright (c) Peter Triesberger
For further information see https://codeberg.org/peter88213/timeline-view-tk
License: GNU GPLv3 (https://www.gnu.org/licenses/gpl-3.0.en.html)
"""
import csv
import os
import unittest

from tlv_model.tlv_csv_file import TlvCsvFile
from tlv_model.tlv_data_model import TlvDataModel
from tlv_model.tlv_section import TlvSection

TEST_CSV_FILE = 'temp/test_dates.csv'
REFERENCE_DATE = '2024-07-13'
NORMAL_DATA_TABLE = [
    ['Title', 'Desc', 'Date', 'Time', 'Day', 'LastsDays', 'LastsHours', 'LastsMinutes'],
    ['TlvSection 5', '', '2024-07-14', '18:56', '', '', '', '20'],
    ['The second event', '', '2024-07-14', '14:15', '', '', '2', ''],
    ['TlvSection 3', '', '2024-07-14', '18:15', '', '', '', '2'],
    ['TlvSection six (no time)', '', '2024-07-14', '', '', '', '', ''],
    ['TlvSection 4', '', '2024-07-14', '18:16', '', '', '', '20'],
    ['TlvSection 1', '', '2024-07-14', '13:00', '', '', '1', '30'],
    ['TlvSection Seven (second day)', '', '', '13:00', '2', '', '1', '30'],
    ['TlvSection Eight (second day, no time)', '', '', '', '2', '', '1', '30'],
    ['TlvSection Nine (time only)', '', '', '18:16', '', '', '1', '30'],
    ['TlvSection Ten (no data)', '', '', '', '', '', '', ''],
    ['', '', '2024-07-13', '', '0', '', '', ''],
]
FAULTY_DATA_TABLE_1 = [
    ['Not a Title', 'Desc', 'Date', 'Time', 'Day', 'LastsDays', 'LastsHours', 'LastsMinutes'],
    ['TlvSection 5', '', '2024-07-14', '18:56', '', '', '', '20'],
]
FAULTY_DATA_TABLE_2 = [
    ['Title', 'Desc', 'Date', 'Time', 'Day', 'LastsDays', 'LastsHours', 'LastsMinutes'],
    ['TlvSection 5', '', '2024-07-14', '18:56'],
]
NORMAL_TEST_SECTIONS = {
    'sc1': TlvSection(
        title='TlvSection 5',
        scDate='2024-07-14',
        scTime='18:56',
        lastsMinutes='20'
        ),
    'sc2': TlvSection(
        title='The second event',
        scDate='2024-07-14',
        scTime='14:15',
        lastsHours='2'
        ),
    'sc3':TlvSection(
        title='TlvSection 3',
        scDate='2024-07-14',
        scTime='18:15',
        lastsMinutes='2'
        ),
    'sc4':TlvSection(
        title='TlvSection six (no time)',
        scDate='2024-07-14',
        ),
    'sc5':TlvSection(
        title='TlvSection 4',
        scDate='2024-07-14',
        scTime='18:16',
        lastsMinutes='20'
        ),
    'sc6':TlvSection(
        title='TlvSection 1',
        scDate='2024-07-14',
        scTime='13:00',
        lastsHours='1',
        lastsMinutes='30',
        ),
    'sc7':TlvSection(
        title='TlvSection Seven (second day)',
        day='2',
        scTime='13:00',
        lastsHours='1',
        lastsMinutes='30',
        ),
    'sc8':TlvSection(
        title='TlvSection Eight (second day, no time)',
        day='2',
        lastsHours='1',
        lastsMinutes='30',
        ),
    'sc9':TlvSection(
        title='TlvSection Nine (time only)',
        scTime='18:16',
        lastsHours='1',
        lastsMinutes='30',
        ),
    'sc10':TlvSection(
        title='TlvSection Ten (no data)',
        ),
}


def create_test_file(dataTable):
    with open(TEST_CSV_FILE, 'w', newline='') as f:
        dataWriter = csv.writer(f, dialect='excel')
        dataWriter.writerows(dataTable)


def delete_test_files():
    try:
        os.remove(TEST_CSV_FILE)
    except FileNotFoundError:
        pass


def get_data_table(filePath):
    with open(filePath, newline='') as f:
        dataReader = csv.reader(f)
        dataTable = list(dataReader)
    return dataTable


class TestNormalWrite(unittest.TestCase):

    def tearDown(self):
        delete_test_files()

    def test_csv_file_write(self):
        model = TlvDataModel(TlvCsvFile)
        model.sections = NORMAL_TEST_SECTIONS
        model.referenceDate = REFERENCE_DATE
        dataFile = TlvCsvFile(model, TEST_CSV_FILE)
        dataFile.write()
        self.assertEqual(get_data_table(TEST_CSV_FILE), NORMAL_DATA_TABLE)

    def test_data_model_write_data(self):
        model = TlvDataModel(TlvCsvFile)
        model.sections = NORMAL_TEST_SECTIONS
        model.referenceDate = REFERENCE_DATE
        model.write_data(TEST_CSV_FILE)
        self.assertEqual(get_data_table(TEST_CSV_FILE), NORMAL_DATA_TABLE)


class TestNormalRead(unittest.TestCase):

    def setUp(self):
        create_test_file(NORMAL_DATA_TABLE)

    def tearDown(self):
        delete_test_files()

    def test_csv_file_read(self):
        model = TlvDataModel(TlvCsvFile)
        dataFile = TlvCsvFile(model, TEST_CSV_FILE)
        dataFile.read()
        for scId in NORMAL_TEST_SECTIONS:
            self.assertEqual(model.sections[scId].title, NORMAL_TEST_SECTIONS[scId].title)
            self.assertEqual(model.sections[scId].desc, NORMAL_TEST_SECTIONS[scId].desc)
            self.assertEqual(model.sections[scId].date, NORMAL_TEST_SECTIONS[scId].date)
            self.assertEqual(model.sections[scId].time, NORMAL_TEST_SECTIONS[scId].time)
            self.assertEqual(model.sections[scId].day, NORMAL_TEST_SECTIONS[scId].day)
            self.assertEqual(model.sections[scId].lastsDays, NORMAL_TEST_SECTIONS[scId].lastsDays)
            self.assertEqual(model.sections[scId].lastsHours, NORMAL_TEST_SECTIONS[scId].lastsHours)
            self.assertEqual(model.sections[scId].lastsMinutes, NORMAL_TEST_SECTIONS[scId].lastsMinutes)
        self.assertEqual(model.referenceDate, REFERENCE_DATE)

    def test_data_model_read_data(self):
        model = TlvDataModel(TlvCsvFile)
        model.read_data(TEST_CSV_FILE)
        for scId in NORMAL_TEST_SECTIONS:
            self.assertEqual(model.sections[scId].title, NORMAL_TEST_SECTIONS[scId].title)
            self.assertEqual(model.sections[scId].desc, NORMAL_TEST_SECTIONS[scId].desc)
            self.assertEqual(model.sections[scId].date, NORMAL_TEST_SECTIONS[scId].date)
            self.assertEqual(model.sections[scId].time, NORMAL_TEST_SECTIONS[scId].time)
            self.assertEqual(model.sections[scId].day, NORMAL_TEST_SECTIONS[scId].day)
            self.assertEqual(model.sections[scId].lastsDays, NORMAL_TEST_SECTIONS[scId].lastsDays)
            self.assertEqual(model.sections[scId].lastsHours, NORMAL_TEST_SECTIONS[scId].lastsHours)
            self.assertEqual(model.sections[scId].lastsMinutes, NORMAL_TEST_SECTIONS[scId].lastsMinutes)
        self.assertEqual(model.referenceDate, REFERENCE_DATE)


class TestFaultyFile1(unittest.TestCase):

    def setUp(self):
        create_test_file(FAULTY_DATA_TABLE_1)

    def tearDown(self):
        delete_test_files()

    def test_data_model_read_data(self):
        model = TlvDataModel(TlvCsvFile)

        with self.assertRaises(ValueError) as exc:
            model.read_data(TEST_CSV_FILE)
        self.assertEqual(str(exc.exception), f'Wrong data structure in "{TEST_CSV_FILE}"')


class TestFaultyFile2(unittest.TestCase):

    def setUp(self):
        create_test_file(FAULTY_DATA_TABLE_2)

    def tearDown(self):
        delete_test_files()

    def test_data_model_read_data(self):
        model = TlvDataModel(TlvCsvFile)

        with self.assertRaises(ValueError) as exc:
            model.read_data(TEST_CSV_FILE)
        self.assertEqual(str(exc.exception), 'not enough values to unpack (expected 8, got 4)')


class TestMissingFile(unittest.TestCase):

    def test_data_model_read_data(self):
        model = TlvDataModel(TlvCsvFile)

        assert not os.path.isfile(TEST_CSV_FILE)
        with self.assertRaises(FileNotFoundError) as exc:
            model.read_data(TEST_CSV_FILE)
        self.assertEqual(str(exc.exception), f"[Errno 2] No such file or directory: '{TEST_CSV_FILE}'")


if __name__ == "__main__":
    unittest.main()
