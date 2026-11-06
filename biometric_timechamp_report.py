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
if os.name == 'nt':
    try:
        os.system(f'title {filename}')
    except Exception:
        pass


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

_HR_DATA_CACHE = {}
_HR_USERS_CACHE = None

def hr_api_call(min_date, max_date):
    cache_key = (str(min_date), str(max_date))
    if cache_key in _HR_DATA_CACHE:
        return _HR_DATA_CACHE[cache_key]
    data = {'from_date': min_date, 'to_date': max_date}
    
    headers = {
        "Cookie": "humans_21909=1",
        "User-Agent": "curl/8.0",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    response = requests.post('https://vservehr.com/attendance_api.php', data=data, headers=headers, timeout=60)
    data = response.json()
    data = data.get('data', [])
    _HR_DATA_CACHE[cache_key] = data
    return data

def get_hr_user_ids():
    global _HR_USERS_CACHE
    if _HR_USERS_CACHE is not None:
        return _HR_USERS_CACHE
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/126.0',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Origin': 'https://vservehr.com',
        'Upgrade-Insecure-Requests': '1',
    }
    
    data = {
        'auth_key': '96223d8b03964041d2adbebb89ef3f8e',
    }
    
    response = requests.post('https://vservehr.com/api_user_list.php', headers=headers, data=data, timeout=60)
    data = response.json()
    data = data.get('data', [])
    _HR_USERS_CACHE = data
    return data

def save_output_file(df, source_type, folder_path):
    """Save the output file with proper naming."""
    valid_dates = pd.to_datetime(df['Date'], errors='coerce').dropna()
    if len(valid_dates) > 0:
        min_date = valid_dates.min()
        max_date = valid_dates.max()
    else:
        min_date = pd.Timestamp.now()
        max_date = pd.Timestamp.now()
    
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

    df['Date'] = pd.to_datetime(df['Date'], errors='coerce', format='mixed')
    # print(df['Date'])
    df['Day'] = df['Date'].dt.strftime('%a')
    df.loc[(df['Day'].isin(['Sat', 'Sun'])) & (df['Worked Hours'].isnull()), 'Attendance'] = 'WeekOff'
    
    valid_dates = df['Date'].dropna()
    if len(valid_dates) > 0:
        min_date = valid_dates.min().strftime('%Y-%m-%d')
        max_date = valid_dates.max().strftime('%Y-%m-%d')
    else:
        min_date = datetime.now().strftime('%Y-%m-%d')
        max_date = datetime.now().strftime('%Y-%m-%d')
    
    df['Date'] = df['Date'].dt.strftime('%Y-%m-%d')
    
    try:
        data = hr_api_call(min_date, max_date)
    except Exception as e:
        raise ValueError(f"Error occurred while calling HR API: {e}")

    hr_user_ids = get_hr_user_ids()
    hr_user_ids = pd.DataFrame(hr_user_ids)
    if 'emp_id' in hr_user_ids.columns:
        hr_user_ids.rename(columns={'emp_id': 'Employee ID', 'designation': 'Designation'}, inplace=True)
    else:
        hr_user_ids = pd.DataFrame(columns=['Employee ID', 'Designation', 'user_name', 'team_name'])

    if not data:
        hr_df = pd.DataFrame(columns=['Date', 'Employee ID', 'HR Attendance'])
    else:
        hr_df = pd.DataFrame(data).astype(str)
        rename_col = {
            'emp_id': 'Employee ID',
            'date': 'Date',
            'attendance': 'HR Attendance',
        }
        hr_df.rename(columns=rename_col, inplace=True)
        for col in ['Date', 'Employee ID', 'HR Attendance']:
            if col not in hr_df.columns:
                hr_df[col] = ''

    df['Employee ID'] = df['Employee ID'].astype(str).str.strip()
    hr_df['Employee ID'] = hr_df['Employee ID'].astype(str).str.strip()

    df['Date'] = pd.to_datetime(df['Date']).dt.date.astype(str)
    df["Employee ID"] = df["Employee ID"].astype(str).str.replace(r"^(10)(\d{2})00$", r"IN", regex=True)
    hr_df['Date'] = pd.to_datetime(hr_df['Date']).dt.date.astype(str)

    df = pd.merge(df, hr_df[['Date', 'Employee ID', 'HR Attendance']], how='left', on=['Employee ID', 'Date'])
    df = pd.merge(hr_user_ids, df, how='left', on='Employee ID')
    df = df[df["Date"].notna()]

    if 'user_name' in df.columns:
        df['Employee Name'] = df['user_name'].fillna(df.get('Employee Name', ''))
    if 'team_name' in df.columns:
        df['Team/Department'] = df['team_name'].fillna(df.get('Team/Department', ''))

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

    pivot_table['No of Worked below 8 hrs'] = pivot_table['Worked Hours Status'].apply(lambda x: 1 if pd.notna(x) and x == 'Worked below 8 hrs' else 0)
    pivot_table['No of Worked below 4 hrs'] = pivot_table['Worked Hours Status'].apply(lambda x: 1 if pd.notna(x) and x == 'Worked below 4 hrs' else 0)
    pivot_table['No of More Break Hours'] = pivot_table['Break Hours Status'].apply(lambda x: 1 if pd.notna(x) and isinstance(x, str) else 0)
    pivot_table['No of Attendance Mismatch'] = pivot_table['Attendance Mismatch'].apply(lambda x: 1 if pd.notna(x) and isinstance(x, str) else 0)

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
    if 'SL No.' in highlight_data.columns:
        highlight_data = highlight_data.drop('SL No.', axis=1)
    highlight_data.insert(0, 'SL No.', range(1, len(highlight_data) + 1))
    
    highlight_data = highlight_data.style.apply(break_highlight_status, axis=1)
    if 'Attendance' in highlight_data.columns:
        highlight_data = highlight_data.map(Attendance_highlight_status, subset=['Attendance'])
    if 'Date' in highlight_data.columns:
        highlight_data = highlight_data.map(Date_highlight_status, subset=['Date'])
    return highlight_data


def process_biometric_data(df):
    print("Stage: processing biometric attendance data", flush=True)
    df['Date'] = pd.to_datetime(df['Date'], format='%Y-%m-%d', dayfirst=True).dt.strftime('%Y-%m-%d')
    df['Shift'] = df['Shift'].apply(normalize_shift)
    
    df = df.reset_index()
    maindf = df.copy()
    
    # Process punch records safely
    valid_punch_mask = df['Punch Records'].notnull() & (df['Punch Records'].astype(str).str.strip().ne(''))
    punch_df = df[valid_punch_mask].copy()

    if len(punch_df) > 0:
        in_out_df = punch_df['Punch Records'].apply(extract_in_out_events)
        in_columns = [col for col in in_out_df.columns if col.startswith('IN')]
        out_columns = [col for col in in_out_df.columns if col.startswith('OUT')]

        in_columns_sorted = sorted(in_columns, key=lambda x: int(x[2:])) if in_columns else []
        out_columns_sorted = sorted(out_columns, key=lambda x: int(x[3:])) if out_columns else []

        interleaved_columns = [col for pair in zip(in_columns_sorted, out_columns_sorted) for col in pair]
        if len(in_columns_sorted) > len(out_columns_sorted):
            interleaved_columns += in_columns_sorted[len(out_columns_sorted):]
        elif len(out_columns_sorted) > len(in_columns_sorted):
            interleaved_columns += out_columns_sorted[len(in_columns_sorted):]

        final_df = pd.concat([punch_df, in_out_df], axis=1)
        sub_df = final_df[['index', 'Date', 'Shift'] + interleaved_columns]

        # Adjust timestamps
        time_columns = [col for col in sub_df.columns if "IN" in col or "OUT" in col]
        for idx, row in sub_df.iterrows():
            date_str = row['Date']
            for col in time_columns:
                sub_df.at[idx, col] = adjust_timestamp(row[col], date_str)

        # Calculate First IN and Last OUT safely
        if in_columns_sorted:
            sub_df['First_IN'] = sub_df[in_columns_sorted[0]]
        else:
            sub_df['First_IN'] = pd.NaT

        if out_columns_sorted:
            reversed_out_columns = sub_df[out_columns_sorted].iloc[:, ::-1]
            sub_df['Last_OUT'] = reversed_out_columns.apply(find_last_non_nan, axis=1)
        else:
            sub_df['Last_OUT'] = pd.NaT

        # Count IN and OUT times
        if in_columns or out_columns:
            sub_df[["IN_Time Count", "OUT_Time Count"]] = sub_df.apply(count_in_out, axis=1).tolist()
        else:
            sub_df["IN_Time Count"] = 0
            sub_df["OUT_Time Count"] = 0

        sub_df.columns = sub_df.columns.str.strip()

        # Calculate work and break hours
        for i in range(1, 30):
            out_col1 = f'OUT {i}'
            in_col1 = f'IN {i}'
            out_col = f'OUT {i}'
            in_col = f'IN {i+1}'
            if out_col1 in sub_df.columns and in_col1 in sub_df.columns:
                sub_df[f'Work_{i}'] = (pd.to_datetime(sub_df[out_col1], format='%Y-%m-%d %H:%M', errors='coerce') - pd.to_datetime(sub_df[in_col1], format='%Y-%m-%d %H:%M', errors='coerce')).dt.total_seconds() // 60
            if in_col in sub_df.columns and out_col in sub_df.columns:
                sub_df[f'Break_{i}'] = (pd.to_datetime(sub_df[in_col], format='%Y-%m-%d %H:%M', errors='coerce') - pd.to_datetime(sub_df[out_col], format='%Y-%m-%d %H:%M', errors='coerce')).dt.total_seconds() // 60

        break_columns = [col for col in sub_df.columns if "Break_" in col]
        work_columns = [col for col in sub_df.columns if "Work_" in col]

        if work_columns:
            sub_df['Worked Hours'] = pd.to_timedelta(sub_df[work_columns].sum(axis=1), unit='m')
        else:
            sub_df['Worked Hours'] = pd.NaT

        if break_columns:
            sub_df['Break Hours'] = pd.to_timedelta(sub_df[break_columns].sum(axis=1), unit='m')
        else:
            sub_df['Break Hours'] = pd.NaT

        # Calculate status and attendance
        if in_columns or out_columns:
            sub_df['Comments'] = sub_df.apply(OutPunch_status, axis=1)
        else:
            sub_df['Comments'] = pd.NaT

        # Calculate break hours
        sub_df['Worked Hours (Number)'] = sub_df['Worked Hours'].apply(lambda x: round(pd.Timedelta(x).total_seconds() / 3600, 2) if pd.notna(x) else pd.NaT)
        sub_df['Break Hours (Number)'] = sub_df['Break Hours'].apply(lambda x: round(pd.Timedelta(x).total_seconds() / 3600, 2) if pd.notna(x) else pd.NaT)

        processed_punch = sub_df[['index', 'First_IN', 'Last_OUT', 'IN_Time Count', 'OUT_Time Count', 'Worked Hours', 'Break Hours', 'Comments', 'Worked Hours (Number)', 'Break Hours (Number)']]
    else:
        processed_punch = pd.DataFrame(columns=[
            'index', 'First_IN', 'Last_OUT', 'IN_Time Count', 'OUT_Time Count',
            'Worked Hours', 'Break Hours', 'Comments', 'Worked Hours (Number)', 'Break Hours (Number)'
        ])

    # Merge with main dataframe
    df = pd.merge(maindf, processed_punch, how='left', on='index')
    df.rename(columns={'Team': 'Team Name'}, inplace=True)
    df['Source'] = 'Biometric'

    df['No of Breaks (Biometric)'] = df['IN_Time Count'].fillna(0).astype(int) - 1
    df.loc[df['No of Breaks (Biometric)'] < 0, 'No of Breaks (Biometric)'] = 0

    shift_col = 'Shift_x' if 'Shift_x' in df.columns else 'Shift'
    df['Shift Timing'] = df[shift_col].apply(convert_shift_format)
    df['Actual Shift Time'] = df['Shift Timing']
    
    df['Shift Start'] = df['Shift Timing'].apply(get_shift_start)

    df['No of Break Counts more than 4 Times (Biometric)'] = (df['No of Breaks (Biometric)'].gt(4).map({True: 'Yes', False: pd.NA}))

    if 'First_IN' in df.columns and 'Shift Start' in df.columns:
        df['Late Login Status'] = df.apply(lambda row: calculate_late_login(row.get('First_IN'), row.get('Shift Start')), axis=1)
    else:
        df['Late Login Status'] = pd.NaT

    if 'First_IN' in df.columns:
        df['Login Time'] = pd.to_datetime(df['First_IN'], errors='coerce').dt.strftime("%H:%M")
    else:
        df['Login Time'] = pd.NaT

    if 'Last_OUT' in df.columns:
        df['Logout Time'] = pd.to_datetime(df['Last_OUT'], errors='coerce').dt.strftime("%H:%M")
    else:
        df['Logout Time'] = pd.NaT

    df['Idle Hours'] = pd.NaT
    df['Away Hours'] = pd.NaT

    rename_col = {
        'Employee Code': 'Employee ID',
        'Team Name': 'Team/Department',
        'Date_x': 'Date',
    }
    df.rename(columns=rename_col, inplace=True)
    if 'Date' not in df.columns and 'Date_y' in df.columns:
        df.rename(columns={'Date_y': 'Date'}, inplace=True)

    df = final_adjustments(df)

    return df


def process_timechamp_data(df):
    print("Stage: processing timechamp attendance data", flush=True)
    # df['Date']
    try:
        dt_parsed = pd.to_datetime(df['Date'].astype(str).str.strip(), errors='coerce', format='mixed')
        if dt_parsed.isna().all():
            dt_parsed = pd.to_datetime(df['Date'].astype(str).str.strip(), format='%b-%d-%Y', errors='coerce')
        valid_dt = dt_parsed.notna()
        df.loc[valid_dt, 'Date'] = dt_parsed[valid_dt].dt.strftime('%Y-%m-%d')
    except Exception as e:
        df['Date'] = pd.to_datetime(df['Date'], errors='coerce', format='mixed').dt.strftime('%Y-%m-%d')
    # print(df['Date'])

    df['Source'] = 'Timechamp'
    if 'User' in df.columns and 'Employee Name' not in df.columns:
        df.rename(columns={'User': 'Employee Name'}, inplace=True)
    if 'Employee ID' in df.columns and 'Employee Id' not in df.columns:
        df.rename(columns={'Employee ID': 'Employee Id'}, inplace=True)
    if 'Team' in df.columns and 'Team Name' not in df.columns:
        df.rename(columns={'Team': 'Team Name'}, inplace=True)
    elif 'Department' in df.columns and 'Team Name' not in df.columns:
        df.rename(columns={'Department': 'Team Name'}, inplace=True)

    # Supply default column values if optional/time columns missing
    for c in ['In Time', 'Out Time', 'Away Time', 'Idle Time', 'Break Time', 'Working Hours', 'Shift']:
        if c not in df.columns:
            df[c] = ''

    try:
        df = df[['Date', 'Employee Id', 'Employee Name', 'Team Name', 'Source', 'Shift', 'In Time', 'Out Time', 'Working Hours', 'Away Time', 'Idle Time', 'Break Time']]
    except Exception as e:
        err_msg = f"Error: Input file is not valid or Missing required columns: {str(e).replace(' not in index', '')}"
        print(err_msg, flush=True)
        raise ValueError(err_msg)
    
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
        try:
            start, end = str(shift).split(' - ')
            start_time = datetime.strptime(start.strip(), '%H:%M IST')
            end_time = datetime.strptime(end.strip(), '%H:%M IST')
            actual_start = (start_time + timedelta(minutes=30)).strftime('%H:%M')
            actual_end = (end_time - timedelta(minutes=30)).strftime('%H:%M')
            return f"{actual_start} IST - {actual_end} IST"
        except Exception:
            return str(shift) if pd.notna(shift) else pd.NaT

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
        err_msg = f"Error: Input file is not valid or Missing required columns: {str(e).replace(' not in index', '')}"
        print(err_msg, flush=True)
        raise ValueError(err_msg)
    
    df['Worked Hours'] = df['Manual Hours'].apply(clean_time_string)
    df['Break Hours'] = df['Break Hours'].apply(clean_time_string)

    # Calculate break hours
    df['Worked Hours (Number)'] = df['Worked Hours'].apply(lambda x: round(pd.Timedelta(x).total_seconds() / 3600, 2) if x else pd.NaT)
    df['Break Hours (Number)'] = df['Break Hours'].apply(lambda x: round(pd.Timedelta(x).total_seconds() / 3600, 2) if x else pd.NaT)
    df['Shift Start'] = df['Shift Timing'].apply(get_shift_start)
    
    def get_actual_shift_time(shift):
        if pd.isna(shift):
            return pd.NaT
        try:
            start, end = str(shift).split(' - ')
            start_time = datetime.strptime(start.strip(), '%H:%M IST')
            end_time = datetime.strptime(end.strip(), '%H:%M IST')
            actual_start = (start_time + timedelta(minutes=30)).strftime('%H:%M')
            actual_end = (end_time - timedelta(minutes=30)).strftime('%H:%M')
            return f"{actual_start} IST - {actual_end} IST"
        except Exception:
            return str(shift) if pd.notna(shift) else pd.NaT

    df['Actual Shift Time'] = df['Shift Timing'].apply(get_actual_shift_time)
    df['Late Login Status'] = df.apply(lambda row: calculate_late_login(row['Login Time'], row['Shift Start']), axis=1)
    df['Idle Hours'] = pd.NaT
    df['Away Hours'] = pd.NaT
    df['No of Break Counts more than 4 Times (Biometric)'] = pd.NaT
    df['No of Breaks (Biometric)'] = pd.NaT
    df['Comments'] = pd.NaT
    df = final_adjustments(df)
    return df

def load_and_process_file(infile):
    """Load an input file and return (raw_df, detected_type).
    
    Returns:
        df: unstyled pandas DataFrame with standardized report columns.
        source_type: 'biometric', 'timechamp', 'manual', or 'report'
    """
    f_name = os.path.basename(infile)
    if 'csv' in infile.lower() or infile.lower().endswith('.csv'):
        with open(infile, 'rb') as f:
            rawdata = f.read()
        encoding_result = charset_normalizer.from_bytes(rawdata).best()
        encoding = encoding_result.encoding if encoding_result else 'utf-8'
        df = pd.read_csv(infile, sep=None, engine='python', encoding=encoding, dtype=str)
    elif 'xls' in infile.lower() or infile.lower().endswith('.xlsx') or infile.lower().endswith('.xls'):
        with pd.ExcelFile(infile) as excel_file:
            if 'Detailed Report' in excel_file.sheet_names:
                df = pd.read_excel(excel_file, sheet_name='Detailed Report', dtype=str)
            else:
                df = pd.read_excel(excel_file, dtype=str)
    else:
        raise ValueError(f"Unsupported file format: {f_name}")

    # Clean up column names and whitespace
    df.columns = [str(c).strip() for c in df.columns]
    df = df.map(lambda x: x.strip() if isinstance(x, str) else x)
    for column in df.columns:
        df[column] = df[column].replace(r'^\s+|\s+$', '', regex=True)

    print(f"Loaded input file: {f_name} ({len(df)} rows)", flush=True)

    # Process Biometric Raw Data
    if 'Punch Records' in df.columns:
        processed = process_biometric_data(df)
        raw_df = processed.data if hasattr(processed, 'data') else processed
        return raw_df.copy(), 'biometric'

    # Process Timechamp Raw Data
    lower_cols = [c.lower() for c in df.columns]
    if "Expected Hours" in df.columns or "Working hours" in df.columns or (
        'employeeid' in lower_cols and any(x in df.columns for x in ['Total Time', 'Working Hours', 'Break Time'])
    ):
        processed = process_timechamp_data(df)
        raw_df = processed.data if hasattr(processed, 'data') else processed
        return raw_df.copy(), 'timechamp'

    # Process Manual Time Raw Data
    if 'Manual Hours' in df.columns:
        processed = process_manualtime_data(df)
        raw_df = processed.data if hasattr(processed, 'data') else processed
        return raw_df.copy(), 'manual'

    # Already-generated report
    if 'Attendance' in df.columns and any(c in df.columns for c in ['Worked Hours (Number)', 'Attendance Mismatch', 'Date']):
        return df.copy(), 'report'

    raise ValueError(
        f"Unrecognised input format: '{f_name}' — "
        "expected a Biometric CSV, Timechamp export, Manual-time file, or an already-generated report."
    )


def _dedup_and_sort_combined(combined_df):
    """Deduplicate overlapping records across files comparing full row and sort."""
    if 'SL No.' in combined_df.columns:
        combined_df = combined_df.drop('SL No.', axis=1)

    for sc in combined_df.select_dtypes(include=['object', 'string']).columns:
        combined_df[sc] = combined_df[sc].apply(lambda v: v.strip() if isinstance(v, str) else v)

    before_dedup = len(combined_df)
    combined_df = combined_df.drop_duplicates(keep='first')
    dedup_removed = before_dedup - len(combined_df)
    if dedup_removed > 0:
        print(f"Ignored {dedup_removed} duplicate row(s) (full row match).", flush=True)

    sort_cols = [c for c in ['Employee ID', 'Employee Name', 'Date'] if c in combined_df.columns]
    if sort_cols:
        combined_df = combined_df.sort_values(by=sort_cols)
    combined_df.insert(0, 'SL No.', range(1, len(combined_df) + 1))
    return combined_df


def file_consolidate(infiles):
    print("Stage: consolidating input files", flush=True)
    processed_list = []
    for infile in infiles:
        df_item, _ = load_and_process_file(infile)
        processed_list.append(df_item)
    combined_df = pd.concat(processed_list, ignore_index=True)
    combined_df = _dedup_and_sort_combined(combined_df)

    # Apply styling df
    styled_df = combined_df.style.apply(break_highlight_status, axis=1)
    if 'Attendance' in combined_df.columns:
        styled_df = styled_df.map(Attendance_highlight_status, subset=['Attendance'])
    if 'Date' in combined_df.columns:
        styled_df = styled_df.map(Date_highlight_status, subset=['Date'])
    return styled_df


def run_report(infiles, base_dir=None, fancy_progress=True):
    """Run the attendance report pipeline - single-pass processing & styling.
    
    infiles: list of input file paths (one or more raw data files, or several already-generated reports).
             When both Biometric and Timechamp raw files are provided, both are processed in memory,
             consolidated, and styled only once at the end.
    base_dir: where the Year/Month/<range> output folder is created (None = current working directory).
    fancy_progress: legacy argument (kept for compatibility).
    Returns the full path to the generated Excel report.
    """
    if not infiles:
        raise ValueError("No input files provided for report generation.")

    if len(infiles) == 1:
        raw_df, detected_type = load_and_process_file(infiles[0])
        combined_df = raw_df
        if detected_type == 'biometric':
            source_type = 'Biometric Attendance'
        elif detected_type == 'timechamp':
            source_type = 'Timechamp Attendance'
        elif detected_type == 'manual':
            source_type = 'Manualtime Attendance'
        else:
            source_type = 'Biometric and Timechamp Consolidated'
    else:
        print(f"Stage: loading and consolidating {len(infiles)} source data files in memory...", flush=True)
        processed_list = []
        types_found = set()
        for infile in infiles:
            raw_df, detected_type = load_and_process_file(infile)
            processed_list.append(raw_df)
            types_found.add(detected_type)

        combined_df = pd.concat(processed_list, ignore_index=True)
        combined_df = _dedup_and_sort_combined(combined_df)

        if types_found == {'biometric'}:
            source_type = 'Biometric Attendance'
        elif types_found == {'timechamp'}:
            source_type = 'Timechamp Attendance'
        elif types_found == {'manual'}:
            source_type = 'Manualtime Attendance'
        else:
            source_type = 'Biometric and Timechamp Consolidated'

    # Apply styling df ONCE for the final workbook
    styled_df = combined_df.style.apply(break_highlight_status, axis=1)
    if 'Attendance' in combined_df.columns:
        styled_df = styled_df.map(Attendance_highlight_status, subset=['Attendance'])
    if 'Date' in combined_df.columns:
        styled_df = styled_df.map(Date_highlight_status, subset=['Date'])

    _rows = len(combined_df)
    print(f"Input processed: {source_type} ({_rows} rows)", flush=True)
    print("Building pivot table (Overall Summary)...", flush=True)
    pivot_table = create_pivot_table(styled_df)
    print("Building highlighted users sheet...", flush=True)
    highlight_data = create_highlighted_data(styled_df)
    print("Writing styled Excel workbook (3 sheets)...", flush=True)

    df_date = combined_df.copy()
    valid_dates = pd.to_datetime(df_date['Date'], errors='coerce', format='mixed').dropna()
    if len(valid_dates) > 0:
        min_date = valid_dates.min()
        max_date = valid_dates.max()
    else:
        min_date = pd.Timestamp.now()
        max_date = pd.Timestamp.now()
    min_date_str = min_date.strftime('%d-%b-%Y')
    max_date_str = max_date.strftime('%d-%b-%Y')

    # Create output directory
    year = max_date.strftime('%Y')
    month = max_date.strftime('%B')
    
    base = os.path.abspath(base_dir) if base_dir else os.getcwd()
    if min_date_str != max_date_str:
        folder_path = os.path.join(base, year, month, f"{min_date_str} to {max_date_str}")
    else:
        folder_path = os.path.join(base, year, month, min_date_str)
    
    os.makedirs(folder_path, exist_ok=True)

    # Save to Excel
    file_name = save_output_file(df_date, source_type, folder_path)

    # Create ExcelWriter with openpyxl engine
    with pd.ExcelWriter(file_name, engine='openpyxl') as writer:
        styled_df.to_excel(writer, index=False, sheet_name='Detailed Report')
        highlight_data.to_excel(writer, index=False, sheet_name='Highlighted Users')
        pivot_table.to_excel(writer, index=False, sheet_name='Overall Summary')

        workbook = writer.book
        side = Side(border_style='dotted', color='000000')
        border_style = Border(left=side, right=side, top=side, bottom=side)

        header_font = Font(name='Verdana', size=9, bold=True)
        header_fill = PatternFill(start_color='9bc2e6', end_color='9bc2e6', fill_type='solid')
        header_align = Alignment(vertical='bottom', horizontal='left')

        body_font = Font(name='Verdana', size=9, color='000000')
        center_align = Alignment(vertical='center', horizontal='center')
        left_align = Alignment(vertical='bottom', horizontal='left')

        center_aligned_columns = {
            'No of Worked Days', 'No of Attendance Mismatch', 'No of Worked Days (Attendance)',
            'No of Break Hours more than 1 Hours', 'No of late login', 'No of Early Logout',
            'Total HR Attendance Hours', 'No of Break Counts more than 4 Times (Biometric)',
            'SL No.', 'Login Time', 'Logout Time', 'No of Breaks (Biometric)', 'Attendance',
            'Day', 'Worked Hours (Number)', 'Break Hours (Number)', 'No of Worked below 4 hrs',
            'No of Worked below 8 hrs', 'No of More Break Hours', 'Away Hours'
        }

        for sheet_name in ['Detailed Report', 'Highlighted Users', 'Overall Summary']:
            worksheet = workbook[sheet_name]

            # Style setup
            if sheet_name == 'Detailed Report':
                worksheet.sheet_properties.tabColor = 'CCFFCC'  # Set tab color to light green
            elif sheet_name == 'Highlighted Users':
                worksheet.sheet_properties.tabColor = 'FFCCCC'  # Set tab color to light red
            elif sheet_name == 'Overall Summary':
                worksheet.sheet_properties.tabColor = 'FFD700'  # Set tab color to gold

            # Apply styles to header
            for cell in worksheet[1]:
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_align
                cell.border = border_style

            center_col_indices = {
                cell.column for cell in worksheet[1] if cell.value in center_aligned_columns
            }

            for row in worksheet.iter_rows(min_row=2, max_row=worksheet.max_row, min_col=1, max_col=worksheet.max_column):
                for cell in row:
                    cell.font = body_font
                    cell.border = border_style
                    cell.alignment = center_align if cell.column in center_col_indices else left_align
            print(f"Styling {sheet_name}: done", flush=True)

    print(f"Report saved: {os.path.basename(file_name)}", flush=True)
    print(f"Saved Location: {file_name}", flush=True)
    print("Successfully Completed!", flush=True)
    return file_name


def main():
    """Original console entry point - behaviour unchanged from the pre-UI script."""
    infiles = [r"G:\My Drive\Attendance Workbench Raw and Report Files\2026\october\rawdata\timechamp_attendance_2026-10-01_2026-10-31.xlsx", r"G:\My Drive\Attendance Workbench Raw and Report Files\2026\october\rawdata\biometric_daily_2026-10-01_2026-10-31.csv"]
    run_report(infiles)
    # os.system('pause')


if __name__ == '__main__':
    main()
