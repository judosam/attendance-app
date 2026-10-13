import re

import charset_normalizer
import pandas as pd
# import re
from datetime import datetime, timedelta
from openpyxl.styles import Font, Border, Alignment, PatternFill, Side
# from pygments import highlight
# import chardet
import os
import requests
import numpy as np
import sys
import json
import pytz
import warnings
import time

# Ignore warnings
warnings.filterwarnings("ignore")

# Get current date and time
now = datetime.now()
dt1 = now.strftime("%b-%d")

# Get the filename of the current script
filename = os.path.splitext(os.path.basename(__file__))[0]
os.system(f'title {filename}')


# Function to extract in and out events from punch records
def extract_in_out_events(punch_records):
    matches = re.findall(r'(\d{2}:\d{2}):(in|out)', punch_records)
    in_events = [time for time, status in matches if status == 'in']
    out_events = [time for time, status in matches if status == 'out']
    
    in_out_dict = {}
    
    for i, in_time in enumerate(in_events):
        in_out_dict[f'IN {i+1}'] = in_time if in_time else pd.NaT
    
    for i, out_time in enumerate(out_events):
        in_out_dict[f'OUT {i+1}'] = out_time if out_time else pd.NaT
    
    return pd.Series(in_out_dict)

# Function to adjust timestamp
def adjust_timestamp(time, date_str):
    if isinstance(time, str):
        timestamp = f"{date_str} {time}"
        timestamp_dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M")
        if timestamp_dt.time() < datetime.strptime("07:00", "%H:%M").time():
            timestamp_dt += timedelta(days=1)
        return timestamp_dt.strftime("%Y-%m-%d %H:%M")
    return pd.NaT

# Function to find last non-NaN value in a row
def find_last_non_nan(row):
    for value in row:
        if not pd.isna(value):
            return value
    return pd.NaT

# Function to count in and out times
def count_in_out(row):
    in_columns = [col for col in row.index if 'IN ' in col]
    out_columns = [col for col in row.index if 'OUT ' in col]
    in_count = sum(1 for col in in_columns if pd.notna(row[col]))
    out_count = sum(1 for col in out_columns if pd.notna(row[col]))
    return in_count, out_count

# Function to calculate work time
def work_time(val):
    first_in = pd.to_datetime(val['First_IN'])
    last_out = pd.to_datetime(val['Last_OUT'])
    if pd.isnull(first_in) or pd.isnull(last_out):
        return pd.NaT
    total_worktime = int((last_out - first_in).total_seconds() // 60)
    return total_worktime

# Function to convert number to hours and minutes
def number_to_hr(time_str):
    if pd.isna(time_str):
        return pd.NaT
    total_seconds = time_str.total_seconds()
    minutes = total_seconds // 60
    hours = int(minutes // 60)
    remaining_minutes = int(minutes % 60)
    remaining_seconds = int(total_seconds % 60)

    if hours and remaining_minutes and remaining_seconds:
        return f"{hours} hrs {remaining_minutes} mins"
    elif hours and remaining_minutes:
        return f"{hours} hrs {remaining_minutes} mins"
    elif hours and remaining_seconds:
        return f"{hours} hrs {remaining_seconds} secs"      
    elif remaining_minutes and remaining_seconds:
        return f"{remaining_minutes} mins {remaining_seconds} secs"
    elif hours:
        return f"{hours} hrs"
    elif remaining_minutes:
        return f"{remaining_minutes} mins"
    elif remaining_seconds:
        return f"{remaining_seconds} secs"
    return pd.NaT

# Function to check if out punch is missing
def OutPunch_status(val):
    if val['IN_Time Count'] != val['OUT_Time Count']:
        return 'User No OutPunch'

# Function to determine attendance status
def attendance_determine_status(team, worked_hours):
    hours = float(worked_hours) if not pd.isna(worked_hours) else 0
    
    if team in ('Rollkall', 'TE'):
        if hours == 0:
            return 'Absent'
        elif 3.75 <= hours <= 7.24:
            return 'Half day'
        elif hours >= 7.25:
            return 'Full day'
        else:
            return 'Absent'
    else:
        if hours == 0:
            return 'Absent'
        elif 3.75 <= hours < 7.74:
            return 'Half day'
        elif hours >= 7.75:
            return 'Full day'
        else:
            return 'Absent'
        

# Function to check if worked hours are below 7.5
def below_7_5_hours(team, hours):
    if pd.isna(hours):
        return pd.NA
    hours = float(hours)
    if team in ('Rollkall', 'TE'):
        if 0 <= hours < 3.75:
            return 'Worked below 4 hrs'
        elif 3.75 <= hours < 7.25:
            return 'Worked below 8 hrs'
    else:
        if 0 <= hours < 3.75:
            return 'Worked below 4 hrs'
        elif 3.75 <= hours < 7.75:
            return 'Worked below 8 hrs'
    # return 'Other'
        
# Function to check if break is greater than one hour
def is_break_greater_than_one_hour(team, break_hours):
    if pd.isna(break_hours):
        return pd.NA
    hours = float(break_hours)
    if team in ('Rollkall', 'TE'):
        return 'Break more than 1 hr 30 mins' if hours >= 1.59 else pd.NaT
    else:
        return 'Break more than 1 hr' if hours > 1.17 else pd.NaT

# Function to convert shift format
def convert_shift_format(shift):
    if pd.isna(shift):
        return pd.NaT

    shift = str(shift).strip()

    if shift == 'GS':
        return '09:00 IST - 18:00 IST'

    if shift == 'NS':
        return '22:00 IST - 06:00 IST'

    # Already normalized
    if 'IST' in shift and ' - ' in shift:
        return shift

    return shift

# Function to get shift start time
def get_shift_start(shift_str):
    if isinstance(shift_str, str):
        start_time_str = shift_str.split(' - ')[0].strip().replace('IST', '').strip()
        try:
            return pd.to_datetime(start_time_str, format='%H:%M').time()
        except ValueError:
            return pd.NaT
    return pd.NaT

def calculate_late_login(in_time_str, shift_start_time):
    if pd.isna(in_time_str) or pd.isna(shift_start_time):
        return pd.NaT

    in_time = pd.to_datetime(in_time_str).time()

    today = datetime.today().date()
    shift_dt = datetime.combine(today, shift_start_time) + timedelta(minutes=30)
    login_dt = datetime.combine(today, in_time)

    # Handle overnight shift
    if shift_start_time.hour >= 18 and in_time.hour < 12:
        login_dt += timedelta(days=1)

    late_time = login_dt - shift_dt

    if late_time < timedelta(0):
        return "Early Login"
    
    if late_time <= timedelta(minutes=5):
        return "No Delay"


    total_mins = int(late_time.total_seconds() // 60)
    hrs, mins = divmod(total_mins, 60)

    if hrs and mins:
        return f"{hrs} hrs {mins} mins late"
    elif hrs:
        return f"{hrs} hrs late"
    else:
        return f"{mins} mins late"

# Function to highlight status based on value
def highlight_status(val, status):
    """Apply styling based on Break Hours Status."""
    if isinstance(status, str):  # Check if status is a string
        return 'background-color: red'  # Change color as needed
    return ''

def break_highlight_status(row):
    if isinstance(row['Break Hours Status'], str):  # Check if "Break Hours Status" is a string
        return ['background-color: red' if col == 'Break Hours' else '' for col in row.index]
    return [''] * len(row)  # No color if condition is not met

# Function to highlight attendance status
def Attendance_highlight_status(value):
    if 'Full day' in value:
        return 'background-color: #89fe79; color: black'
    elif 'Half day' in value:
        return 'background-color: #ffb3b3; color: black'
    elif 'Absent' in value:
        return 'background-color: #fe7d79; color: black'
    elif 'WeekOff' in value:
        return 'background-color: #ffd700; color: black'

# Function to highlight date status (weekends)
def Date_highlight_status(val):
    date = datetime.strptime(val, '%Y-%m-%d')
    if date.weekday() in [5, 6]:
        if date.weekday() == 5:
            day_of_month = date.day
            week_of_month = (day_of_month - 1) // 7 + 1
            if week_of_month in [1, 3]:
                return 'background-color: #FFD700; color: black'
        else:
            return 'background-color: #FFD700; color: black'

# Function to clean time string
def clean_time_string(time_str):
    try:
        if pd.isna(time_str) or time_str == '':
            return pd.NaT
        return pd.to_timedelta(str(time_str))
    except Exception:
        return pd.NaT

# Function to convert IST time(s) to CST
def convert_to_cst(time_value, team):
    """
    Convert IST time / shift to US Central Time (DST safe).
    Works for:
    - Shift ranges (09:00 IST - 18:00 IST)
    - Single times (09:00)
    Applies only for Rollkall team.
    """

    # Skip non-Rollkall or empty
    if team != 'Rollkall' or pd.isna(time_value):
        return time_value

    ist_tz = pytz.timezone('Asia/Kolkata')
    cst_tz = pytz.timezone('US/Central')

    value = str(time_value).strip()

    # Handle NS shift
    # value = value.replace('NS', '22:00 - 06:00')
    if ' - ' in value:
        value = value.replace('IST', '')
        start_str, end_str = value.split(' - ')

        start_dt_ist = ist_tz.localize(datetime.strptime(start_str.strip(), "%H:%M"))
        end_dt_ist = ist_tz.localize(datetime.strptime(end_str.strip(), "%H:%M"))

        start_cst = start_dt_ist.astimezone(cst_tz)
        end_cst = end_dt_ist.astimezone(cst_tz)
        return f"{start_cst.strftime('%H:%M CST')} - {end_cst.strftime('%H:%M CST')}"
    else:
        single_dt_ist = ist_tz.localize(datetime.strptime(value.replace('IST', '').strip(), "%H:%M"))
        single_cst = single_dt_ist.astimezone(cst_tz)
        return single_cst.strftime("%H:%M")

def parse_time_string(x):
    if not isinstance(x, str):
        return 0
    
    parts = x.split()
    hours, minutes, seconds = 0, 0, 0
    
    for i in range(0, len(parts), 2):
        value = int(parts[i])
        unit = parts[i+1] if i+1 < len(parts) else 'secs'
        if 'hr' in unit:
            hours = value
        elif 'min' in unit:
            minutes = value
        elif 'sec' in unit:
            seconds = value
    
    return pd.Timedelta(hours=hours, minutes=minutes, seconds=seconds)

# Define a function for mapping
def map_late_login_status(status):
    if pd.isna(status) or status in ['Early Login', 'No Delay']:
        return 0
    return 1

def normalize_shift(value):
    if pd.isna(value):
        return value

    s = str(value).strip().upper()
    s = re.sub(r'\s+', ' ', s)

    if s == "GS":
        return "09:00 IST - 18:00 IST"

    if s == "NS":
        return "22:00 IST - 06:00 IST"

    if s == "XMEK":
        return "XMEK"

    m = re.search(r'(\d{1,2})\.(\d{2}).*?TO\s*(\d{1,2})\.(\d{2})', s)

    if m:
        sh, sm, eh, em = m.groups()
        return f"{int(sh):02d}:{sm} IST - {int(eh):02d}:{em} IST"

    return s

def hr_api_call(min_date, max_date):
    data = {'from_date': min_date, 'to_date': max_date}
    
    headers = {
        "Cookie": "humans_21909=1",
        "User-Agent": "curl/8.0",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    response = requests.post('https://vservehr.com/attendance_api.php', data=data, headers=headers)
    data = response.json()
    data = data['data']
    return data

def get_hr_user_ids():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/126.0',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Origin': 'https://vservehr.com',
        'Upgrade-Insecure-Requests': '1',
    }
    
    data = {
        'auth_key': '96223d8b03964041d2adbebb89ef3f8e',
    }
    
    response = requests.post('https://vservehr.com/api_user_list.php', headers=headers, data=data)
    data = response.json()
    # print(data)
    data = data['data']
    return data

def save_output_file(df, source_type, folder_path):
    """Save the output file with proper naming."""
    min_date = df['Date'].min()
    max_date = df['Date'].max()
    
    if isinstance(min_date, str):
        min_date = pd.to_datetime(min_date)
    if isinstance(max_date, str):
        max_date = pd.to_datetime(max_date)
    
    min_date_str = min_date.strftime('%d-%b-%Y')
    max_date_str = max_date.strftime('%d-%b-%Y')
    
    if min_date_str != max_date_str:
        file_name = f"{folder_path}/{source_type} Report {min_date_str} to {max_date_str}.xlsx"
    else:
        file_name = f"{folder_path}/{source_type} Report {min_date_str}.xlsx"
    
    return file_name

def attendance_mismatch(attendance_row, hr_attendance_row, half_leaves):
    if hr_attendance_row == 'Present' and attendance_row in ['Half day','Absent','WeekOff']:
        return 'YES'
    elif hr_attendance_row in half_leaves and attendance_row in ['Absent', 'WeekOff']:
        return 'YES'
    elif hr_attendance_row == 'No Data':
        return 'YES'
    else:
        return pd.NaT

def calculate_early_logout(out_time_str, shift, grace_min=5):
    if pd.isna(out_time_str) or pd.isna(shift):
        return pd.NaT

    try:
        start_str, end_str = [re.sub(r'[A-Z]+', '', x).strip() for x in shift.split(' - ')]

        shift_start = datetime.strptime(start_str, "%H:%M")
        shift_end = datetime.strptime(end_str, "%H:%M")
        out_time = datetime.strptime(out_time_str, "%H:%M")

    except:
        return pd.NaT

    today = datetime.today().date()

    shift_start = datetime.combine(today, shift_start.time())
    shift_end = datetime.combine(today, shift_end.time())
    out_time = datetime.combine(today, out_time.time())

    # Overnight shift
    if shift_end <= shift_start:
        shift_end += timedelta(days=1)

        # Logout after midnight
        if out_time.hour < 12:
            out_time += timedelta(days=1)

    diff = shift_end - out_time

    if diff <= timedelta(minutes=grace_min):
        return "No Early Logout"

    hours, remainder = divmod(int(diff.total_seconds()), 3600)
    minutes = remainder // 60

    if hours and minutes:
        return f"{hours} hrs {minutes} mins early"
    elif hours:
        return f"{hours} hrs early"

    return f"{minutes} mins early"

def final_adjustments(df):
    df['Attendance'] = df.apply(lambda row: attendance_determine_status(row['Team/Department'], row['Worked Hours (Number)']), axis=1)
    df['Break Hours Status'] = df.apply(lambda row: is_break_greater_than_one_hour(row['Team/Department'], row['Break Hours (Number)']), axis=1)
    df['Worked Hours Status'] = df.apply(lambda row: below_7_5_hours(row['Team/Department'], row['Worked Hours (Number)']), axis=1)
    
    for column in ['Worked Hours', 'Idle Hours', 'Break Hours']:
        df[column] = df[column].apply(number_to_hr)
    
    df.loc[df['Shift Timing'].isin(['00:00 IST - 23:55 IST', '12:00 IST - 11:55 IST','00:00 IST - 23:59 IST']), 'Actual Shift Time'] = df['Shift Timing']
    
    df['Early Logout Status'] = df.apply(lambda row: calculate_early_logout(row['Logout Time'], row['Actual Shift Time']), axis=1)
    df.loc[df['Shift Timing'].isin(['00:00 IST - 23:55 IST', '12:00 IST - 11:55 IST','00:00 IST - 23:59 IST']), 'Late Login Status'] = pd.NaT
    df.loc[df['Shift Timing'].isin(['00:00 IST - 23:55 IST', '12:00 IST - 11:55 IST','00:00 IST - 23:59 IST']), 'Early Logout Status'] = pd.NaT
    # df.loc[df['Employee ID'].isin(['1271', '1121', '1122', '1070', '1272', '1231', '1331', '1081', '1016', '1037', '1020', '1260', '1206', '1071', '1129', '1098', '1042', '1061', '1229', '1087', '1058', '1056']), 'Late Login Status'] = pd.NaT
    # df.loc[df['Attendance'].isin(['Absent','Half day','WeekOff']), 'Late Login Status'] = pd.NaT
    # df.loc[(df['Worked Hours (Number)'] < 1) | (df['Worked Hours (Number)'].isnull()), 'Late Login Status'] = pd.NaT
    # df.loc[(df['Worked Hours (Number)'] < 1) | (df['Worked Hours (Number)'].isnull()), 'Early Logout Status'] = pd.NaT

    # df.loc[df['Employee ID'].isin(['1271', '1121', '1122', '1070', '1272', '1231', '1331', '1081', '1016', '1037', '1020', '1260', '1206', '1071', '1129', '1098', '1042', '1061', '1229', '1087', '1058', '1056']), 'Early Logout Status'] = pd.NaT
    # df.loc[df['Attendance'].isin(['Absent','Half day','WeekOff']), 'Early Logout Status'] = pd.NaT

    df['Date'] = pd.to_datetime(df['Date'], format='%Y-%m-%d', errors='coerce')
    # print(df['Date'])
    df['Day'] = df['Date'].dt.strftime('%a')
    df.loc[(df['Day'].isin(['Sat', 'Sun'])) & (df['Worked Hours'].isnull()), 'Attendance'] = 'WeekOff'
    
    min_date = df['Date'].min().strftime('%Y-%m-%d')
    max_date = df['Date'].max().strftime('%Y-%m-%d')
    
    df['Date'] = df['Date'].dt.strftime('%Y-%m-%d')
    
    try:
        data = hr_api_call(min_date, max_date)
    except Exception as e:
        raise ValueError(f"Error occurred while calling HR API: {e}")
        # data = []

    hr_user_ids = get_hr_user_ids()
    hr_user_ids =  pd.DataFrame(hr_user_ids)
    hr_user_ids.rename(columns={'emp_id': 'Employee ID', 'designation': 'Designation'}, inplace=True)
    # hr_user_ids.to_excel('debug_hr_user_ids.xlsx', index=False)

    hr_df = pd.DataFrame(data).astype(str)  
    # print(hr_user_ids)

    rename_col = {
        'emp_id': 'Employee ID',
        'date': 'Date',
        'attendance': 'HR Attendance',
    }

    hr_df.rename(columns=rename_col, inplace=True)
    df['Employee ID'] = df['Employee ID'].astype(str).str.strip()
    hr_df['Employee ID'] = hr_df['Employee ID'].astype(str).str.strip()

    df['Date'] = pd.to_datetime(df['Date']).dt.date.astype(str)
    df["Employee ID"] = df["Employee ID"].astype(str).str.replace(r"^(10)(\d{2})00$", r"IN\1\2", regex=True)
    hr_df['Date'] = pd.to_datetime(hr_df['Date']).dt.date.astype(str)

    df = pd.merge(df, hr_df[['Date', 'Employee ID', 'HR Attendance']], how='left', on=['Employee ID', 'Date'])
    df = pd.merge(hr_user_ids, df, how='left', on='Employee ID')
    df = df[df["Date"].notna()]

    df['Employee Name'] = df['user_name']
    df['Team/Department'] = df['team_name']

    # df.to_excel('debug_merged.xlsx', index=False)

    # df.loc[df['team_name'].notna() & df['team_name'].ne(''), 'Team/Department'] = df['team_name']
    # dd
    # emp_id = hr_df['Employee ID'].unique().tolist()
    df['HR Attendance'] = df['HR Attendance'].fillna('No Data')

    # df['HR Attendance'] = df['HR Attendance'].where(df['HR Attendance'].notna() | df['Employee ID'].isin(emp_id), 'No Data')

    leave_shifts = [
        'Leave - Full Day', 'Absent - Full Day', 'Sick Leave - Full',
        'EL FD', 'Maternity Leave', 'Bereavement Leave', 'Week Off',
        'Paternity Leave', 'Adoption Leave','Paid Holiday','Comp Off'
    ]

    cols_to_null = [
        'Login Time', 'Logout Time', 'Late Login Status', 'Worked Hours', 'Worked Hours (Number)',
        'Idle Hours', 'Away Hours', 'Break Hours', 'Break Hours (Number)', 'Worked Hours Status',
        'Break Hours Status', 'No of Breaks (Biometric)', 'No of Break Counts more than 4 Times (Biometric)',
        'Comments'
    ]

    df.loc[df['HR Attendance'].isin(leave_shifts), cols_to_null] = np.nan
        
    half_leaves = ['Sick Leave - Half', 'EL HD', 'Leave - Half Day', 'Absent - Half Day']

    df.loc[df['HR Attendance'].isin(half_leaves) & (df['Worked Hours Status'] == 'Worked below 8 hrs'), 'Worked Hours Status'] = np.nan
    df.loc[df['HR Attendance'].isin(half_leaves), 'Break Hours Status'] = np.nan

    df.loc[df['HR Attendance'].isin(half_leaves), 'Early Logout Status'] = np.nan
    df.loc[df['HR Attendance'].isin(half_leaves), 'Late Login Status'] = np.nan

    df.loc[(df['Worked Hours (Number)'].fillna(0) < 1) & (df['HR Attendance'] != 'Present'), 'Late Login Status'] = pd.NaT
    df.loc[(df['Worked Hours (Number)'].fillna(0) < 1) & (df['HR Attendance'] != 'Present'), 'Early Logout Status'] = pd.NaT
    
    # df.loc[df['HR Attendance'] != 'Present', 'Early Logout Status'] = pd.NaT
    # df.loc[df['HR Attendance'] != 'Present', 'Late Login Status'] = pd.NaT

    df['Attendance Mismatch'] = df.apply(lambda row: attendance_mismatch(row['Attendance'], row['HR Attendance'], half_leaves), axis=1)

    df = df[['Date', 'Day', 'Employee ID', 'Employee Name', 'Team/Department', 'Designation',
            'Source', 'Attendance', 'HR Attendance', 'Attendance Mismatch', 'Shift Timing',
            'Actual Shift Time', 'Login Time', 'Logout Time', 'Late Login Status',
            'Early Logout Status', 'Worked Hours','Worked Hours (Number)','Worked Hours Status',
            'Break Hours', 'Break Hours (Number)', 'Break Hours Status','Idle Hours','Away Hours',
            'No of Breaks (Biometric)','No of Break Counts more than 4 Times (Biometric)', 'Comments']]
    
    # Columns to apply conversion
    columns_to_convert = ['Shift Timing', 'Actual Shift Time', 'Login Time', 'Logout Time']

    # Apply conversion
    for col in columns_to_convert:
        df[col] = df.apply(lambda row: convert_to_cst(row[col], row['Team/Department']), axis=1)
        
    df['Attendance'].fillna('Absent', inplace=True)
    df = df.replace(0, pd.NaT)

    df['Employee Name'] = df['Employee Name'].replace(r'\s+', ' ', regex=True).str.strip()
    df = df.sort_values(by=['Employee Name', 'Date'])
    df = df.drop_duplicates()
    df.insert(0, 'SL No.', range(1, len(df) + 1))
    
    # Apply styling df
    df = df.style.apply(break_highlight_status, axis=1)
    df = df.map(Attendance_highlight_status, subset=['Attendance'])
    df = df.map(Date_highlight_status, subset=['Date'])
    return df


def create_pivot_table(df):
    # Create a pivot table
    pivot_table = df.data.copy()
    attendance_mapping = {'Absent': 0,'WeekOff': 0,'Full day': 1,'Half day': 0.5}

    pivot_table['No of Worked Days'] = pivot_table['Attendance'].map(attendance_mapping)

    pivot_table['No of Worked below 8 hrs'] = pivot_table['Worked Hours Status'].apply(lambda x: 1 if x == 'Worked below 8 hrs' else 0)
    pivot_table['No of Worked below 4 hrs'] = pivot_table['Worked Hours Status'].apply(lambda x: 1 if x == 'Worked below 4 hrs' else 0)
    pivot_table['No of More Break Hours'] = pivot_table['Break Hours Status'].apply(lambda x: 1 if isinstance(x, str) else 0)
    pivot_table['No of Attendance Mismatch'] = pivot_table['Attendance Mismatch'].apply(lambda x: 1 if isinstance(x, str) else 0)

    half_leaves = [
        'Sick Leave - Half', 'EL HD', 'Leave - Half Day',
        'Absent - Half Day'
    ]

    def hr_attendance(value):
        if value in half_leaves:
            return 0.5 * 8
        elif value == 'Present':
            return 1 * 8
        return 0

    def hr_attendance_count(value):
        if value in half_leaves:
            return 0.5
        elif value == 'Present':
            return 1
        return 0
    
    pivot_table['Total HR Attendance Hours'] = pivot_table['HR Attendance'].apply(hr_attendance)
    pivot_table['No of Worked Days (Attendance)'] = pivot_table['HR Attendance'].apply(hr_attendance_count)

    # Use apply() with a lambda function to map values
    pivot_table['No of late login'] = pivot_table['Late Login Status'].apply(map_late_login_status)
    pivot_table['No of Early Logout'] = pivot_table['Early Logout Status'].apply(lambda x: 0 if pd.isna(x) or x == 'No Early Logout' else 1)
    
    # Replace non-numeric values with 0, then convert to int
    pivot_table['No of Breaks (Biometric)'] = pivot_table['No of Breaks (Biometric)'].replace('', 0).fillna(0).astype(int)
    pivot_table['No of Break Counts more than 4 Times (Biometric)'] = pivot_table['No of Breaks (Biometric)'].apply(lambda x: 1 if x > 4 else 0)

    pivot_table['Total Worked Hours'] = pivot_table['Worked Hours'].apply(parse_time_string)
    pivot_table['Total Break Hours'] = pivot_table['Break Hours'].apply(parse_time_string)
    
    # pivot_table.to_excel('debug_pivot_processed.xlsx', index=False)
    time_cols = ['Total Worked Hours', 'Total Break Hours']
    for col in time_cols:
        pivot_table[col] = (pd.to_timedelta(pivot_table[col], unit='D', errors='coerce'))
        
    pivot_table = pivot_table.groupby(['Employee ID', 'Employee Name', 'Team/Department', 'Source']).agg({
        'No of Worked Days': 'sum',
        'No of Worked Days (Attendance)': 'sum',        
        'No of late login': 'sum',
        'No of Early Logout': 'sum',
        'No of Worked below 4 hrs': 'sum',
        'No of Worked below 8 hrs': 'sum',
        'No of More Break Hours': 'sum',
        'No of Break Counts more than 4 Times (Biometric)': 'sum',
        'Total HR Attendance Hours': 'sum',
        'Total Worked Hours': 'sum',
        'Total Break Hours': 'sum',
    }).fillna(0)

    pivot_table = pivot_table.reset_index()

    pivot_table['Total HR Attendance Hours'] = (pd.to_timedelta(pivot_table['Total HR Attendance Hours'], unit='h', errors='coerce'))
    pivot_table['Worked Hours Diff'] = (pivot_table['Total Worked Hours'] - pivot_table['Total HR Attendance Hours'])
    pivot_table['Worked Hours Diff'] = pivot_table['Worked Hours Diff'].apply(lambda x: round(x.total_seconds() / 3600, 2) if pd.notna(x) else pd.NaT)
    pivot_table['Total Worked Hours'] = pivot_table['Total Worked Hours'].apply(lambda x: round(x.total_seconds() / 3600, 2) if pd.notna(x) else pd.NaT)
    pivot_table['Total Break Hours'] = pivot_table['Total Break Hours'].apply(lambda x: round(x.total_seconds() / 3600, 2) if pd.notna(x) else pd.NaT)
    pivot_table['Total HR Attendance Hours'] = pivot_table['Total HR Attendance Hours'].apply(lambda x: round(x.total_seconds() / 3600, 2) if pd.notna(x) else pd.NaT)

    pivot_table.insert(0, 'SL No.', range(1, len(pivot_table) + 1))
    return pivot_table

def create_highlighted_data(df):
    highlight_data = df.data.copy()
    highlight_data = highlight_data.replace({'Early Login': pd.NaT, 'No Delay': pd.NaT, 'No Early Logout': pd.NaT})
    highlight_data = highlight_data.dropna(subset=['Late Login Status', 'Early Logout Status', 'Worked Hours Status', 'Break Hours Status', 'No of Break Counts more than 4 Times (Biometric)','Attendance Mismatch'], how='all')
    highlight_data = highlight_data.drop('SL No.', axis=1)
    highlight_data.insert(0, 'SL No.', range(1, len(highlight_data) + 1))
    
    highlight_data = highlight_data.style.apply(break_highlight_status, axis=1)
    highlight_data = highlight_data.map(Attendance_highlight_status, subset=['Attendance'])
    highlight_data = highlight_data.map(Date_highlight_status, subset=['Date'])
    return highlight_data

def process_biometric_data(df):
    print("Stage: processing biometric attendance data", flush=True)
    # print(df['Date'])
    df['Date'] = pd.to_datetime(df['Date'], format='%Y-%m-%d', dayfirst=True).dt.strftime('%Y-%m-%d')
    df['Shift'] = df['Shift'].apply(normalize_shift)
    
    df = df.reset_index()
    maindf = df.copy()
    
    # Process punch records
    df = df[df['Punch Records'].notnull()]
    in_out_df = df['Punch Records'].apply(extract_in_out_events)
    final_df = pd.concat([df, in_out_df], axis=1)

    # Sort and interleave IN and OUT columns
    in_columns = [col for col in final_df.columns if col.startswith('IN')]
    out_columns = [col for col in final_df.columns if col.startswith('OUT')]

    in_columns_sorted = sorted(in_columns, key=lambda x: int(x[2:]))
    out_columns_sorted = sorted(out_columns, key=lambda x: int(x[3:]))

    interleaved_columns = [col for pair in zip(in_columns_sorted, out_columns_sorted) for col in pair]

    if len(in_columns_sorted) > len(out_columns_sorted):
        interleaved_columns += in_columns_sorted[len(out_columns_sorted):]
    elif len(out_columns_sorted) > len(in_columns_sorted):
        interleaved_columns += out_columns_sorted[len(in_columns_sorted):]

    df = final_df[['index', 'Date', 'Shift'] + interleaved_columns]

    # Adjust timestamps
    time_columns = [col for col in df.columns if "IN" in col or "OUT" in col]
    for idx, row in df.iterrows():
        date_str = row['Date']
        for col in time_columns:
            df.at[idx, col] = adjust_timestamp(row[col], date_str)

    # Calculate First IN and Last OUT
    df['First_IN'] = df.filter(like='IN').iloc[:, 0]
    out_columns = df.filter(like='OUT')
    reversed_out_columns = out_columns.iloc[:, ::-1]
    df['Last_OUT'] = reversed_out_columns.apply(find_last_non_nan, axis=1)

    # Count IN and OUT times
    df[["IN_Time Count", "OUT_Time Count"]] = df.apply(count_in_out, axis=1).tolist()

    df.columns = df.columns.str.strip()

    # Calculate work and break hours
    for i in range(1, 30):
        out_col = f'OUT {i}'
        in_col = f'IN {i+1}'
        out_col1 = f'OUT {i}'
        in_col1 = f'IN {i}'
        if out_col1 in df.columns:
            df[f'Work_{i}'] = (pd.to_datetime(df[out_col1], format='%Y-%m-%d %H:%M') - pd.to_datetime(df[in_col1], format='%Y-%m-%d %H:%M')).dt.total_seconds() // 60
        if in_col in df.columns:
            df[f'Break_{i}'] = (pd.to_datetime(df[in_col], format='%Y-%m-%d %H:%M') - pd.to_datetime(df[out_col], format='%Y-%m-%d %H:%M')).dt.total_seconds() // 60

    break_columns = [col for col in df.columns if "Break_" in col]
    work_columns = [col for col in df.columns if "Work_" in col]

    df['Worked Hours'] = df[work_columns].sum(axis=1)
    df['Break Hours'] = df[break_columns].sum(axis=1)
    df['Worked Hours'] = pd.to_timedelta(df['Worked Hours'], unit='m')
    df['Break Hours'] = pd.to_timedelta(df['Break Hours'], unit='m')

    # Calculate status and attendance
    df['Comments'] = df.apply(OutPunch_status, axis=1)

    # Calculate break hours
    df['Worked Hours (Number)'] = df['Worked Hours'].apply(lambda x: round(pd.Timedelta(x).total_seconds() / 3600, 2) if x else pd.NaT)
    df['Break Hours (Number)'] = df['Break Hours'].apply(lambda x: round(pd.Timedelta(x).total_seconds() / 3600, 2) if x else pd.NaT)

    # Merge with main dataframe
    df = pd.merge(maindf, df, how='left', on='index')
    df.rename(columns={'Team': 'Team Name'}, inplace=True)
    df['Source'] = 'Biometric'

    df['No of Breaks (Biometric)'] = df['IN_Time Count'] - 1

    df['Shift Timing'] = df['Shift_x'].apply(convert_shift_format)
    df['Actual Shift Time'] = df['Shift Timing']
    
    df['Shift Start'] = df['Shift Timing'].apply(get_shift_start)

    # df['No of Break Counts more than 4 Times (Biometric)'] = df['IN_Time Count'].replace('', 0).fillna(0).astype(int)
    # df['IN_Time Count'] = pd.to_numeric(df['IN_Time Count'],errors='coerce')
    df['No of Break Counts more than 4 Times (Biometric)'] = (df['No of Breaks (Biometric)'].gt(4).map({True: 'Yes', False: pd.NA}))

    # Function to calculate late login
    def calculate_late_login(in_time_str, shift_start_time):
        if pd.isna(in_time_str) or pd.isna(shift_start_time):
            return pd.NaT

        in_time = pd.to_datetime(in_time_str).time()
        shift_start_dt = datetime.combine(datetime.today(), shift_start_time)
        shift_start_with_grace = shift_start_dt #+ timedelta(minutes=5)

        in_time_dt = datetime.combine(datetime.today(), in_time)
        late_minutes = in_time_dt - shift_start_with_grace

        if in_time_dt > shift_start_with_grace:
            hours = int(late_minutes.total_seconds() // 3600)
            minutes = int((late_minutes.total_seconds() % 3600) // 60)
            remaining_minutes = int(minutes % 60)
            if late_minutes > timedelta(minutes=5):
                if hours and remaining_minutes:
                    return f"{hours} hrs {remaining_minutes} mins late"
                elif not hours and remaining_minutes:
                    return f"{remaining_minutes} mins late"
                elif hours and not remaining_minutes:
                    return f"{hours} hrs late"
            else:
                return 'No Delay'
        elif in_time_dt < shift_start_with_grace:
            return 'Early Login'
        else:
            return 'No Delay'
    
    df['Late Login Status'] = df.apply(lambda row: calculate_late_login(row['First_IN'], row['Shift Start']), axis=1)

    df['Login Time'] = pd.to_datetime(df['First_IN']).dt.strftime("%H:%M")
    df['Logout Time'] = pd.to_datetime(df['Last_OUT']).dt.strftime("%H:%M")

    df['Idle Hours'] = pd.NaT
    df['Away Hours'] = pd.NaT

    rename_col = {
        'Employee Code': 'Employee ID',
        'Team Name': 'Team/Department',
        'Date_x': 'Date',
    }
    df.rename(columns=rename_col, inplace=True)

    df = final_adjustments(df)

    return df

def process_timechamp_data(df):
    print("Stage: processing timechamp attendance data", flush=True)
    # df['Date']
    try:
        df['Date'] = pd.to_datetime(df['Date'].str.strip(), format='%b-%d-%Y', errors='coerce')
        df['Date'] = df['Date'].dt.strftime('%Y-%m-%d')
    except Exception as e:
        df['Date'] = pd.to_datetime(df['Date'], errors='coerce', format='mixed').dt.strftime('%Y-%m-%d')
    # print(df['Date'])

    df['Source'] = 'Timechamp'
    df.rename(columns={'User': 'Employee Name'}, inplace=True)

    try:
        df = df[['Date', 'Employee Id', 'Employee Name', 'Team Name', 'Source', 'Shift', 'In Time', 'Out Time', 'Working Hours', 'Away Time', 'Idle Time', 'Break Time']]
    except Exception as e:
        print(f"Error: Input file is not valid or Missing required columns: {str(e).replace(' not in index', '')}", flush=True)
        os.system('pause')
        sys.exit()
    
    for column in ['Working Hours', 'Idle Time', 'Away Time', 'Break Time']:
        df[column] = df[column].apply(clean_time_string)

    df['Login Time'] = pd.to_datetime(df['In Time'].astype(str), format='mixed').dt.strftime('%H:%M')
    df['Logout Time'] = pd.to_datetime(df['Out Time'].astype(str), format='mixed').dt.strftime('%H:%M')

    # Calculate break hours
    df['Worked Hours (Number)'] = df['Working Hours'].apply(lambda x: round(pd.Timedelta(x).total_seconds() / 3600, 2) if x else pd.NaT)
    df['Break Hours (Number)'] = df['Away Time'].apply(lambda x: round(pd.Timedelta(x).total_seconds() / 3600, 2) if x else pd.NaT)
    df['Break Time'] = df['Break Time'].apply(lambda x: round(pd.Timedelta(x).total_seconds() / 3600, 2) if x else pd.NaT)

    df['Shift Start'] = df['Shift'].apply(get_shift_start)
    
    def get_actual_shift_time(shift):
        if pd.isna(shift):
            return pd.NaT
        # print(shift)
        start, end = shift.split(' - ')
        start_time = datetime.strptime(start, '%H:%M IST')
        end_time = datetime.strptime(end, '%H:%M IST')
        actual_start = (start_time + timedelta(minutes=30)).strftime('%H:%M')
        actual_end = (end_time - timedelta(minutes=30)).strftime('%H:%M')
        return f"{actual_start} IST - {actual_end} IST"

    df['Actual Shift Time'] = df['Shift'].apply(get_actual_shift_time)
    df['Late Login Status'] = df.apply(lambda row: calculate_late_login(row['Login Time'], row['Shift Start']), axis=1)
    df['No of Break Counts more than 4 Times (Biometric)'] = pd.NaT
    df['No of Breaks (Biometric)'] = pd.NaT
    df['Comments'] = pd.NaT

    # Rename columns
    rename_col = {
        'Employee Id': 'Employee ID',
        'Team Name': 'Team/Department',
        'Shift': 'Shift Timing',
        'Idle Time': 'Idle Hours',
        'Break Time': 'Away Hours',
        'Away Time': 'Break Hours',
        'Working Hours': 'Worked Hours',
    }

    df.rename(columns=rename_col, inplace=True)

    df = final_adjustments(df)

    return df

def process_manualtime_data(df):
    print("Stage: processing manual tracking data", flush=True)
    df['Source'] = 'Manual Tracking'
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce').dt.strftime('%Y-%m-%d')

    try:
        df = df[['Date', 'Employee ID', 'Employee Name', 'Team/Department', 'Source', 'Shift Timing', 'Login Time', 'Logout Time', 'Manual Hours', 'Break Hours']]
    except Exception as e:
        print(f"Error: Input file is not valid or Missing required columns: {str(e).replace(' not in index', '')}", flush=True)
        os.system('pause')
        sys.exit()
    
    df['Worked Hours'] = df['Manual Hours'].apply(clean_time_string)
    df['Break Hours'] = df['Break Hours'].apply(clean_time_string)

    # Calculate break hours
    df['Worked Hours (Number)'] = df['Worked Hours'].apply(lambda x: round(pd.Timedelta(x).total_seconds() / 3600, 2) if x else pd.NaT)
    df['Break Hours (Number)'] = df['Break Hours'].apply(lambda x: round(pd.Timedelta(x).total_seconds() / 3600, 2) if x else pd.NaT)
    df['Shift Start'] = df['Shift Timing'].apply(get_shift_start)
    
    def get_actual_shift_time(shift):
        if pd.isna(shift):
            return pd.NaT
        # print(shift)
        start, end = shift.split(' - ')
        start_time = datetime.strptime(start, '%H:%M IST')
        end_time = datetime.strptime(end, '%H:%M IST')
        actual_start = (start_time + timedelta(minutes=30)).strftime('%H:%M')
        actual_end = (end_time - timedelta(minutes=30)).strftime('%H:%M')
        return f"{actual_start} IST - {actual_end} IST"

    df['Actual Shift Time'] = df['Shift Timing'].apply(get_actual_shift_time)
    df['Late Login Status'] = df.apply(lambda row: calculate_late_login(row['Login Time'], row['Shift Start']), axis=1)
    df['Idle Hours'] = pd.NaT
    df['Away Hours'] = pd.NaT
    df['No of Break Counts more than 4 Times (Biometric)'] = pd.NaT
    df['No of Breaks (Biometric)'] = pd.NaT
    df['Comments'] = pd.NaT
    df = final_adjustments(df)
    return df

def file_consolidate(infiles):
    print("Stage: consolidating generated reports", flush=True)
    combined_df = pd.DataFrame()
    for i, infile in enumerate(infiles, 1):
        f_name = os.path.basename(infile)
        print(f"Consolidating file {i}: {f_name}", flush=True)
        if 'csv' in infile:
            df = pd.read_csv(infile, sep=None, engine='python', encoding='utf-8', dtype=str)
        elif 'xls' in infile:
            df = pd.read_excel(infile, dtype=str)
        combined_df = pd.concat([combined_df, df], ignore_index=True)
    print()
    combined_df = combined_df.sort_values(by=['Employee ID', 'Employee Name', 'Date'])
    combined_df.drop('SL No.', axis=1, inplace=True)
    combined_df.insert(0, 'SL No.', range(1, len(combined_df) + 1))
    
    # Apply styling df
    combined_df = combined_df.style.apply(break_highlight_status, axis=1)
    combined_df = combined_df.map(Attendance_highlight_status, subset=['Attendance'])
    combined_df = combined_df.map(Date_highlight_status, subset=['Date'])
    return combined_df


def run_report(infiles, base_dir=None, fancy_progress=True):
    """Run the attendance report pipeline - same logic and output as the original console script.
    infiles: list of input file paths (one raw data file, or several already-generated reports to consolidate).
    base_dir: where the Year/Month/<range> output folder is created (None = current working directory).
    fancy_progress: use the rich console progress bar (True) or simple text progress lines (False).
    Returns the full path to the generated Excel report.
    """
    # infile = r"timechamp dec 2025.xlsx"
    # infile = r"Biometric dec 2025.xlsx"
    # infile = r"Biometric Raw data _Jan_2026 2.xlsx"
    # infile = r"Timechamp raw data _Jan 2026 2.xlsx"

    if len(infiles) == 1:
        infile = infiles[0]
        # Read the input file based on its type
        if 'csv' in infile:
            with open(infile, 'rb') as f:
                rawdata = f.read()
            encoding_result = charset_normalizer.from_bytes(rawdata).best()
            encoding = encoding_result.encoding if encoding_result else 'utf-8'
            df = pd.read_csv(infile, sep=None, engine='python', encoding=encoding, dtype=str)
        elif 'xls' in infile:
            df = pd.read_excel(infile)
        print(f"Loaded input file: {os.path.basename(infile)} ({len(df)} rows)", flush=True)

        # Clean up column names and data
        df = df.rename(columns=lambda x: x.strip())
        df = df.map(lambda x: x.strip() if isinstance(x, str) else x)

        for column in df.columns:
            df[column] = df[column].replace(r'^\s+|\s+$', '', regex=True)

        # Process Biometric Data
        if 'Punch Records' in df.columns:
            df = process_biometric_data(df)
            source_type = 'Biometric Attendance'
        elif "Expected Hours" in df.columns:
            df = process_timechamp_data(df)
            source_type = 'Timechamp Attendance'
        elif 'Manual Hours' in df.columns:
            df = process_manualtime_data(df)
            source_type = 'Manualtime Attendance'
        else:
            raise ValueError(
                f"Unrecognised input format: '{os.path.basename(infile)}' — "
                "expected a Biometric CSV, Timechamp export, or Manual-time file."
            )
    else:
        df = file_consolidate(infiles)
        source_type = 'Biometric and Timechamp Consolidated'

    # Create pivot table and highlighted data
    _rows = len(df.data) if hasattr(df, 'data') else len(df)
    print(f"Input processed: {source_type} ({_rows} rows)", flush=True)
    print("Building pivot table (Overall Summary)...", flush=True)
    pivot_table = create_pivot_table(df)
    print("Building highlighted users sheet...", flush=True)
    highlight_data = create_highlighted_data(df)
    print("Writing styled Excel workbook (3 sheets)...", flush=True)

    df_date = df.data.copy()
    df_date['Date'] = pd.to_datetime(df_date['Date'], format='%Y-%m-%d', dayfirst=True)
    min_date = df_date['Date'].min()
    max_date = df_date['Date'].max()
    min_date_str = min_date.strftime('%d-%b-%Y')
    max_date_str = max_date.strftime('%d-%b-%Y')

    # Create output directory
    year = max_date.strftime('%Y')
    month = max_date.strftime('%B')
    
    base = os.path.abspath(base_dir) if base_dir else os.getcwd()
    if min_date_str != max_date_str:
        folder_path = os.path.join(base, year, month, f"{min_date.strftime('%d-%b-%Y')} to {max_date.strftime('%d-%b-%Y')}")
    else:
        folder_path = os.path.join(base, year, month, min_date.strftime('%d-%b-%Y'))
    
    os.makedirs(folder_path, exist_ok=True)

    # Save to Excel
    file_name = save_output_file(df_date, source_type, folder_path)
    # print(file_name)

    # Create ExcelWriter with openpyxl engine
    with pd.ExcelWriter(file_name, engine='openpyxl') as writer:
        # Write DataFrames to their respective sheets
        df.to_excel(writer, index=False, sheet_name='Detailed Report')
        highlight_data.to_excel(writer, index=False, sheet_name='Highlighted Users')
        pivot_table.to_excel(writer, index=False, sheet_name='Overall Summary')

        workbook = writer.book
        for sheet_name in ['Detailed Report', 'Highlighted Users', 'Overall Summary']:
            worksheet = workbook[sheet_name]

            # Style setup
            if sheet_name == 'Detailed Report':
                worksheet.sheet_properties.tabColor = 'CCFFCC'  # Set tab color to light green
            elif sheet_name == 'Highlighted Users':
                worksheet.sheet_properties.tabColor = 'FFCCCC'  # Set tab color to light red
            elif sheet_name == 'Overall Summary':
                worksheet.sheet_properties.tabColor = 'FFD700'  # Set tab color to gold
            side = Side(border_style='dotted', color='000000')
            border_style = Border(left=side, right=side, top=side, bottom=side)

        
            # Apply styles to header
            for cell in worksheet[1]:
                cell.font = Font(name='Verdana', size=9, bold=True)
                cell.fill = PatternFill(start_color='9bc2e6', end_color='9bc2e6', fill_type='solid')
                cell.alignment = Alignment(vertical='bottom', horizontal='left')
                cell.border = border_style
            
            center_aligned_columns = ['No of Worked Days', 'No of Attendance Mismatch', 'No of Worked Days (Attendance)',
                                'No of Break Hours more than 1 Hours', 'No of late login','No of Early Logout', 'Total HR Attendance Hours',
                                'No of Break Counts more than 4 Times (Biometric)', 'SL No.', 'Login Time',
                                'Logout Time','No of Breaks (Biometric)', 'Attendance', 'Day', 'Worked Hours (Number)',
                                'Break Hours (Number)', 'No of Worked below 4 hrs', 'No of Worked below 8 hrs', 'No of More Break Hours','Away Hours']

            center_aligned_indices = [worksheet.cell(row=1, column=i).column 
                                  for i in range(1, worksheet.max_column + 1) 
                                  if worksheet.cell(row=1, column=i).value in center_aligned_columns]

            for i, row in enumerate(worksheet.iter_rows(min_row=2, max_row=worksheet.max_row, min_col=1, max_col=worksheet.max_column)):
                for cell in row:
                    cell.font = Font(name='Verdana', size=9, color='000000')
                    cell.border = border_style
                    if cell.column in center_aligned_indices:
                        cell.alignment = Alignment(vertical='center', horizontal='center')
                    else:
                        cell.alignment = Alignment(vertical='bottom', horizontal='left')
            print(f"Styling {sheet_name}: done", flush=True)
    print(f"Report saved: {os.path.basename(file_name)}", flush=True)
    print(f"Saved Location: {file_name}", flush=True)
    print("Successfully Completed!", flush=True)
    # messagebox.showinfo("Vserve Biometric Report", "Successfully Completed...")
    return file_name



def main():
    """Original console entry point - behaviour unchanged from the pre-UI script."""
    infiles = [r"G:\My Drive\Attendance Workbench Raw Data\2026\september\rawdata\biometric_daily_2026-09-16_2026-09-16.csv"]
    run_report(infiles)
    # os.system('pause')


if __name__ == '__main__':
    main()
