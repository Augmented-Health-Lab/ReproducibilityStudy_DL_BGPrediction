from __future__ import division, print_function

import collections
import csv
import datetime
import numpy as np
import pandas as pd
import glob 
import os
import sys
import os
import torch

import matplotlib.pyplot as plt
import xml.etree.ElementTree as ET
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F

from datetime import datetime, timedelta
from scipy.interpolate import CubicSpline
from torch.utils.data import DataLoader, TensorDataset
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_squared_error

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
print(device, flush = True)


def preprocess_DiaTrend(path, round=False):
    """
    Preprocess the DiaTrend data from a CSV file.

    Args:
        path (str): Path to the CSV file.
        round (bool): If True, round the timestamps to the nearest 6 minutes.

    Returns:
        list: A list of dictionaries containing the processed data.
    """
    
    subject = pd.read_csv(path)
    subject['date'] = pd.to_datetime(subject['date'], errors='coerce')  # Convert 'date' column to datetime if not already
    subject.sort_values('date', inplace=True)  # Sort the DataFrame by the 'date' column

    if round:
        rounded_timestamp = []
        for ts in subject["date"]:
            rounded_timestamp.append(ts)
        subject["rounded_date"] = rounded_timestamp
        formatted_data = [[{'ts': row['rounded_date'], 'value': row['mg/dl']}] for _, row in subject.iterrows()]
    else:
        # Convert each row to the desired format
        formatted_data = [[{'ts': row['date'].to_pydatetime(), 'value': row['mg/dl']}] for _, row in subject.iterrows()]

    return formatted_data

def segement_data_as_6_min(data, user_id):
    """
    Segments the data into smaller chunks based on time differences.
    
    Args:
        data (pd.DataFrame): The input DataFrame containing the data to be segmented.
        user_id (int): The user ID for naming the segments.
    
    Returns:
        dict: A dictionary where keys are segment names and values are DataFrames of the segments.
    """
    
    df = pd.DataFrame(data)

    # Calculate time differences
    df['time_diff'] = df['timestamp'].diff()

    # Identify large gaps
    df['new_segment'] = df['time_diff'] > pd.Timedelta(hours=0.1)

    # Find indices where new segments start
    segment_starts = df[df['new_segment']].index

    # Initialize an empty dictionary to store segments
    segments = {}
    prev_index = 0

    # Loop through each segment start and slice the DataFrame accordingly
    for i, start in enumerate(segment_starts, 1):
        segments[f'segment_{user_id}_{i}'] = df.iloc[prev_index:start].reset_index(drop=True)
        prev_index = start

    # Add the last segment from the last gap to the end of the DataFrame
    segments[f'segment_{user_id}_{len(segment_starts) + 1}'] = df.iloc[prev_index:].reset_index(drop=True)

    # Optionally remove helper columns from each segment
    for segment in segments.values():
        segment.drop(columns=['time_diff', 'new_segment'], inplace=True)
    
    return segments


def prepare_dataset(segments, history_len):
    '''
    Function to prepare the dataset for training and testing.
    
    Args:
        segments (dict): Dictionary containing segmented data.
        history_len (int): Length of the history to consider for features.

    Returns:
        features_list (list): List of feature arrays.
        raw_glu_list (list): List of raw glucose values.
    
    ph = 6, 30 minutes ahead
    ph = 12, 60 minutes ahead
    '''
    ph = 6
    features_list = []
    labels_list = []
    raw_glu_list = []
    
    
    # Iterate over each segment
    for segment_name, segment_df in segments.items():
       

        # Fill NaNs that might have been introduced by conversion errors
        segment_df.fillna(0, inplace=True)

        # Maximum index for creating a complete feature set
        # print("len of segment_df is ", len(segment_df))
        max_index = len(segment_df) - (history_len + ph)  # Subtracting only 15+ph to ensure i + 15 + ph is within bounds
        
        # Iterate through the data to create feature-label pairs
        for i in range(max_index):
            # Extracting features from index i to i+15
            segment_df = segment_df.reset_index(drop = True)
            features = segment_df.loc[i:i+history_len, ['glucose_value']].values
            raw_glu_list.append(segment_df.loc[i+history_len+ph, 'glucose_value'])
            features_list.append(features)
            # labels_list.append(label)
            
    print("len of features_list " + str(len(features_list)))

    return features_list, raw_glu_list


def get_gdata(filename):
    """
    Function to get glucose data from a file and preprocess it.

    Args:
        filename (str): Path to the file containing glucose data.

    Returns:
        segments (dict): Dictionary containing segmented glucose data.
    """
    
    glucose = preprocess_DiaTrend(filename)
    glucose_dict = {entry[0]['ts']: entry[0]['value'] for entry in glucose}

    # Create the multi-channel database
    g_data = []
    for timestamp in glucose_dict:
        record = {
            'timestamp': timestamp,
            'glucose_value': glucose_dict[timestamp],
            # 'meal_type': None,
            # 'meal_carbs': 0
        }
            
        g_data.append(record)

    # Create DataFrame
    glucose_df = pd.DataFrame(g_data)

    # Convert glucose values to numeric type for analysis
    glucose_df['glucose_value'] = pd.to_numeric(glucose_df['glucose_value'])

    # Calculate percentiles
    lower_percentile = np.percentile(glucose_df['glucose_value'], 2)
    upper_percentile = np.percentile(glucose_df['glucose_value'], 98)

    # Print thresholds
    # print(f"2% lower threshold: {lower_percentile}")
    # print(f"98% upper threshold: {upper_percentile}")
    filename = os.path.basename(j)
    file_number = int(filename.split('Subject')[-1].split('.')[0])  # Extract numeric part before '.
    segments = segement_data_as_6_min(glucose_df, file_number)

    return segments



class WaveNetBlock(nn.Module):
    """
    Class WaveNet Block for dilated convolutions.
    """
    
    def __init__(self, in_channels, dilation):
        """
        Initialize the WaveNet block with two dilated convolutions and a residual connection.
        
        Args:
            in_channels (int): Number of input channels.
            dilation (int): Dilation rate for the convolutions.
        
        Returns:
            None
        
        """
        super(WaveNetBlock, self).__init__()
        self.conv1 = nn.Conv1d(in_channels, in_channels, kernel_size=2, dilation=dilation, padding=1+dilation - 2^(dilation-1))
        self.conv2 = nn.Conv1d(in_channels, in_channels, kernel_size=2, dilation=dilation, padding=dilation)
        self.res_conv = nn.Conv1d(in_channels, in_channels, kernel_size=1)
        
    def forward(self, x):
        """
        Forward pass through the WaveNet block.
        
        Args:
            x (torch.Tensor): Input tensor of shape (batch_size, in_channels, seq_len).
        
        Returns:
            torch.Tensor: Output tensor of the same shape as input.
        """
        # print("shape of x: ", x.shape)
        out = F.relu(self.conv1(x))
        # print("shape of first out: ", out.shape)
        out = F.relu(self.conv2(out))
        # print("shape of second out: ", out.shape)
        res = self.res_conv(x)
        # print("shape of res: ", res.shape)
        return out + res

class WaveNet(nn.Module):
    """
    Class WaveNet for the entire model.
    """
    
    def __init__(self, in_channels, out_channels, num_blocks, dilations):
        """
        Initialize the WaveNet model with an initial convolution, multiple blocks, and final convolutions.
        
        Args:
            in_channels (int): Number of input channels.
            out_channels (int): Number of output channels.
            num_blocks (int): Number of WaveNet blocks.
            dilations (list): List of dilation rates for the blocks.
        
        Returns:
            None
        """
        
        super(WaveNet, self).__init__()
        self.initial_conv = nn.Conv1d(in_channels, 32, kernel_size=2, padding=1)
        self.blocks = nn.ModuleList([WaveNetBlock(32, dilation) for dilation in dilations])
        self.final_conv1 = nn.Conv1d(32, 128, kernel_size=2, padding=0)
        self.final_conv2 = nn.Conv1d(128, 256, kernel_size=2, padding=0)
        self.fc1 = nn.Linear(256, 128)
        self.fc2 = nn.Linear(128, out_channels)
        
    def forward(self, x):
        """
        Input tensor of shape (batch_size, in_channels, seq_len).
        
        Args:
            x (torch.Tensor): Input tensor of shape (batch_size, in_channels, seq_len).
        
        Returns:
            torch.Tensor: Output tensor of shape (batch_size, out_channels).
        """
        
        x = F.relu(self.initial_conv(x))
        for block in self.blocks:
            # print("enter the block loop")
            x = block(x)
        x = F.relu(self.final_conv1(x))
        x = F.relu(self.final_conv2(x))
        x = x[:, :, -1]  # Get the last time step
        x = F.relu(self.fc1(x))
        x = self.fc2(x)
        return x

def get_test_rmse(model, test_loader):
    """
    Function to calculate RMSE on the test set.
    
    Args:
        model (nn.Module): The trained model.
        test_loader (DataLoader): DataLoader for the test set.
    
    Returns:
        float: RMSE value.
    """
    
    model.eval()
    predictions = []
    actuals = []
    with torch.no_grad():
        for inputs, targets in test_loader:
            inputs, targets = inputs.to('cpu'), targets.to('cpu')
            outputs = model(inputs.permute(0, 2, 1))
            predictions.append(outputs)
            actuals.append(targets)

    predictions = torch.cat(predictions).cpu().numpy()
    actuals = torch.cat(actuals).cpu().numpy()


    rmse = np.sqrt(mean_squared_error(actuals,predictions))
    print(f'RMSE on the folds: {rmse}')
    return  rmse



##############################################################################
#                     
#
#                                 TRAINING 
#
#
##############################################################################

def main():     
    """
    Main function to train the WaveNet model on the DiaTrend dataset.
    
    The function processes the data, prepares it for training, and trains the model.
    """
    
    ph = 6
    fold = sys.argv[1]
    history_len = sys.argv[2]

    splits = [0, 11, 22, 33, 44, 54]

    if len(sys.argv) > 2: 
        fold = sys.argv[1]
        bot_range = splits[int(fold) -1]
        top_range = splits[int(fold)]
        history_len = int(sys.argv[2])
        print(f'fold number is {fold} and history length is {history_len}', flush = True)
        print(f'bot range is {bot_range} and top range is {top_range}', flush = True)
    else: 
        fold = 1
        bot_range = splits[0]
        top_range = splits[1]
        history_len = 7 

    segment_list = []
    for j in glob.glob('../diatrend_processed/*.csv'):
        # don't use overlap
        filename = os.path.basename(j)
        file_number = int(filename.split('Subject')[-1].split('.')[0])  # Extract numeric part before '.csv'
        if bot_range <= file_number <= top_range:
            continue
        else: 
            print("Processing train file ", filename, flush=True)
            segments = get_gdata(j)
            segment_list.append(segments)

    # merge the list so that it's one list of dictionaries
    merged_segments = {}
    for segment in segment_list:
        for key, value in segment.items():
            merged_segments[key] = value



    input_channels = 1  # Number of features
    output_channels = 1  # Predicting a single value (glucose level)
    
    if history_len <= 6: 
        num_blocks = 3
    else: 
        num_blocks = 4  # Number of WaveNet blocks
        
    dilations = [2**i for i in range(num_blocks)]  # Dilation rates: 1, 2, 4, 8

    model = WaveNet(input_channels, output_channels, num_blocks, dilations)
    print(model, flush=True)

    # Example of how to define the loss and optimizer
    criterion = nn.MSELoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=0.0008)


    try:
        fold_number = int(sys.argv[1])
        history_len = int(sys.argv[2])
    except:
        fold_number = 1
        history_len = 7
        
    print(f'fold number is {fold_number} and history length is {history_len}', flush = True)
    bolus_updated_segments = merged_segments
    features_list, raw_glu_list = prepare_dataset(bolus_updated_segments, history_len)
    # Assuming features_list and raw_glu_list are already defined
    features_array = np.array(features_list)
    labels_array = np.array(raw_glu_list)

    # Step 1: Split into 80% train+val and 20% test
    X_train, X_val, y_train, y_val = train_test_split(features_array, labels_array, test_size=0.2, shuffle=False)

    # Step 2: Split the 80% into 70% train and 10% val (0.7/0.8 = 0.875)
    # Convert the splits to torch tensors
    X_train = torch.tensor(X_train, dtype=torch.float32)
    y_train = torch.tensor(y_train, dtype=torch.float32)
    X_val = torch.tensor(X_val, dtype=torch.float32)
    y_val = torch.tensor(y_val, dtype=torch.float32)

    # make toch cudnn benchmark true
    torch.backends.cudnn.benchmark = True
    # Create DataLoaders

    train_dataset = TensorDataset(X_train, y_train)
    train_loader = DataLoader(train_dataset, batch_size=128, shuffle=False, pin_memory=True, num_workers=12)

    val_dataset = TensorDataset(X_val, y_val)
    val_loader = DataLoader(val_dataset, batch_size=128, shuffle=False, pin_memory=True, num_workers=12)


    print("Training the model", flush=True)
    # Training Loop
    num_epochs = 100
    for epoch in range(num_epochs):
        model.train()
        for inputs, targets in train_loader:
            optimizer.zero_grad()
            outputs = model(inputs.permute(0, 2, 1))  # Permute to match (batch, channels, seq_len)
            # use squeeze
            outputs = outputs.squeeze()
            targets = targets.squeeze()
            loss = criterion(outputs, targets)
            loss.backward()
            optimizer.step()

        model.eval()
        val_loss = 0
        with torch.no_grad():
            for inputs, targets in val_loader:
                outputs = model(inputs.permute(0, 2, 1))  # Permute to match (batch, channels, seq_len)
                # outputs = outputs.squeeze()
                # targets = targets.squeeze()

                loss = criterion(outputs, targets)
                val_loss += loss.item() 
        
        print(f'Epoch {epoch+1}, Validation Loss: {val_loss / len(val_loader)}', flush=True)
        

    model.eval()
    predictions = []
    actuals = []

    with torch.no_grad():
        for inputs, targets in val_loader:
            inputs, targets = inputs.to('cpu'), targets.to('cpu')
            outputs = model(inputs.permute(0, 2, 1))
            predictions.append(outputs)
            actuals.append(targets)

    predictions = torch.cat(predictions).cpu().numpy()
    actuals = torch.cat(actuals).cpu().numpy()


    rmse = np.sqrt(mean_squared_error(actuals,predictions))
    print(f'RMSE on validation set: {rmse}')

    # save the model c
    torch.save(model.state_dict(), f'./Diatrend_100/GluNet_DIATREND_{fold}_model_{history_len}.pth')


    ##############################################################################
    #
    #                                TESTING
    #
    ##############################################################################


    segment_list = [] 
    test_segment_list = []
    new_test_rmse_list = []

    for j in glob.glob('../diatrend_processed/*.csv'):
        # don't use overlap
        filename = os.path.basename(j)
        file_number = int(filename.split('Subject')[-1].split('.')[0])  # Extract numeric part before '.csv'
        # Exclude files within the range 0 to 248
        if bot_range <= file_number <= top_range:
            print("Processing test file ", filename, flush=True)
            test_segments = get_gdata(j)
            test_features, test_glu = prepare_dataset(test_segments, history_len)
            test_features_array = np.array(test_features)
            test_labels_array = np.array(test_glu)

            X_test = test_features_array
            y_test = test_labels_array

            # Assuming features_list and raw_glu_list are already defined
            X_test = torch.tensor(X_test, dtype=torch.float32)
            y_test = torch.tensor(y_test, dtype=torch.float32)

            test_dataset = TensorDataset(X_test, y_test)
            test_loader = DataLoader(test_dataset, batch_size=128, shuffle=False)
            new_test_rmse_list.append([filename.split('.')[0], get_test_rmse(model, test_loader)])

    df = pd.DataFrame(new_test_rmse_list, columns = ['rmse', 'filenumber']).to_csv(f'Glunet_Diatrend_Fold{fold}_HL{history_len}_rmse.csv', index = False)


if __name__ == "__main__":
    main()