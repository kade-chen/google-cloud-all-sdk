# Mainly used for Google Cloud SDKs service account authentication

## SDK includes the following development languages:

- Python
- Java

## Use Cloud Translation Prerequisites 

### Service Account

1. Create a Google Cloud Project
2. [Enable the Cloud Translation API](https://console.cloud.google.com/flows/enableapi?apiid=translate.googleapis.com)
3. [Enable the Cloud Storage API](https://console.cloud.google.com/apis/library/storage-component.googleapis.com)
4. [Create a Service Account](https://console.cloud.google.com/iam-admin/serviceaccounts/create)
5. Grant the Service Account the `Cloud Translation API` role
6. Create a Key for the Service Account
7. Download the Key as a JSON file
8. Use the JSON file to authenticate the SDK

### Usage

- [Python](/python/CloudTranslate/v2/TranslateServiceAccount.py)
- [Java](/java/src/main/java/CloudTranslation/v2/TranslationServiceAccount.java)